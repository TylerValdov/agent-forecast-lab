"""Nested subsamples, post-cutoff mode, parallel variants, cross-run comparisons.
(Synthetic data, fake LLMs.)"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from forecast_lab.agents.fake import HeuristicFakeLLM
from forecast_lab.ledger.jsonl import Ledger
from forecast_lab.report.build import build_report
from forecast_lab.runner.config import ExperimentConfig
from forecast_lab.runner.prereg import PreregRecord
from forecast_lab.runner.replay import run_replay, select_games

FAR = datetime(2100, 1, 1, tzinfo=UTC)
PRE = PreregRecord("x", "x" * 64, None, False, True)


def cfg(**kw) -> ExperimentConfig:
    base = {"name": "x", "mode": "replay_anonymized", "model": "claude-haiku-5-5",
            "variants": ["A"], "budget": {"usd": 5, "tokens": 10**8},
            "games": {"start": "2030-12-01", "end": "2031-02-01", "sample": 40,
                      "sample_seed": 1},
            "evaluation": {"min_n": 5, "bootstrap_resamples": 200}}
    return ExperimentConfig(**(base | kw))


def test_nested_subsample_is_a_subset(league_store):
    big = select_games(league_store, cfg(), FAR)
    small = select_games(league_store, cfg(games={**cfg().games.model_dump(),
                                                  "subsample": 15, "subsample_seed": 9}), FAR)
    assert len(big) == 40 and len(small) == 15
    assert {g.game_id for g in small} <= {g.game_id for g in big}
    assert small == sorted(small, key=lambda g: (g.tipoff_utc, g.game_id))


def test_post_cutoff_mode_refuses_earlier_games(league_store):
    with pytest.raises(ValidationError):
        cfg(mode="replay_post_cutoff")  # needs post_cutoff_after
    with pytest.raises(ValueError, match="tip off before"):
        select_games(league_store, cfg(mode="replay_post_cutoff",
                                       post_cutoff_after="2031-01-01"), FAR)
    ok = select_games(league_store, cfg(mode="replay_post_cutoff",
                                        post_cutoff_after="2030-12-01"), FAR)
    assert len(ok) == 40


def test_post_cutoff_runs_with_real_names(league_store, tmp_path):
    c = cfg(mode="replay_post_cutoff", post_cutoff_after="2030-12-01",
            games={"start": "2030-12-01", "end": "2030-12-05"})
    out = run_replay(c, PRE, league_store, HeuristicFakeLLM(), out_root=tmp_path)
    agent = [r for r in Ledger(out / "predictions.jsonl").verify() if r["kind"] == "agent"]
    assert agent and all(r["mode"] == "replay_post_cutoff" for r in agent)
    assert all(r["home"] in r["trajectory"][0]["text"] for r in agent)  # real names shown


def test_parallel_matches_sequential(league_store, tmp_path):
    kw = {"variants": ["A", "B", "C", "D"], "hints": {"rate": 0.3, "seed": 2}}
    seq = run_replay(cfg(concurrency=1, **kw), PRE, league_store, HeuristicFakeLLM(),
                     out_root=tmp_path / "s")
    par = run_replay(cfg(concurrency=4, **kw), PRE, league_store, HeuristicFakeLLM(),
                     out_root=tmp_path / "p")

    def key(out):
        recs = Ledger(out / "predictions.jsonl").verify()
        return [(r["game_id"], r["variant"], r["prob"]) for r in recs if r["kind"] == "agent"]

    assert key(seq) == key(par) and len(key(par)) > 100
    for r in Ledger(par / "predictions.jsonl").verify():
        for n in r.get("loaded_memory") or []:
            assert n["created_at"] < r["as_of"]  # memory still time-gated in parallel


def test_external_comparison_in_report(league_store, tmp_path):
    a = cfg(name="haiku_x", variants=["A"])
    run_replay(a, PRE, league_store, HeuristicFakeLLM(), out_root=tmp_path)
    b = cfg(name="big_x", variants=["A"],
            games={**a.games.model_dump(), "subsample": 20, "subsample_seed": 3},
            evaluation={"min_n": 5, "bootstrap_resamples": 200,
                        "external": [{"experiment": "haiku_x", "variant": "A",
                                      "label": "haiku_A"}],
                        "comparisons": [{"a": "A", "b": "haiku_A", "rq": "model size"}]})
    out = run_replay(b, PRE, league_store, HeuristicFakeLLM(), out_root=tmp_path)
    text = Path(build_report(tmp_path, tmp_path / "R.md", run_dirs=[out])).read_text()
    assert "haiku_A" in text and "agent_A vs haiku_A" in text
    row = next(line for line in text.splitlines() if "agent_A vs haiku_A" in line)
    assert "| 20 |" in row  # paired on the 20 shared games
