"""End-to-end runner tests with a fake LLM on synthetic data."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from forecast_lab.agents.fake import HeuristicFakeLLM
from forecast_lab.ledger.jsonl import Ledger
from forecast_lab.report.build import build_report
from forecast_lab.runner.config import ExperimentConfig
from forecast_lab.runner.live import live_predict, live_resolve
from forecast_lab.runner.prereg import PreregistrationError, PreregRecord, check_preregistered
from forecast_lab.runner.replay import run_replay

CFG = {
    "name": "t", "mode": "replay_anonymized", "model": "claude-haiku-5-5",
    "variants": ["A", "B", "C", "D"],
    "games": {"start": "2030-12-15", "end": "2031-01-10", "sample": 30, "sample_seed": 1},
    "hints": {"rate": 0.3, "seed": 2}, "budget": {"usd": 5, "tokens": 10**8},
    "evaluation": {"min_n": 10, "bootstrap_resamples": 500,
                   "comparisons": [{"a": "B", "b": "A", "rq": "RQ1"},
                                   {"a": "D", "b": "A", "rq": "RQ3"}]},
}
PREREG = PreregRecord("t.yaml", "f" * 64, None, False, True)


def test_replay_order_and_artifacts(league_store, tmp_path):
    cfg = ExperimentConfig(**CFG)
    out = run_replay(cfg, PREREG, league_store, HeuristicFakeLLM(), out_root=tmp_path)
    preds = Ledger(out / "predictions.jsonl").verify()
    outs = Ledger(out / "outcomes.jsonl").verify()
    manifest = json.loads((out / "manifest.json").read_text())
    agent = [p for p in preds if p["kind"] == "agent"]
    assert manifest["stopped_reason"] == "completed" and manifest["prereg"]["exploratory"]
    assert {p["variant"] for p in agent} == {"A", "B", "C", "D"}
    assert len(outs) == 30 and all(p["prob"] is not None for p in agent)
    # Every outcome is written after every prediction for that game.
    written = {r["game_id"]: r["written_at"] for r in outs}
    assert all(r["timestamp"] <= written[r["game_id"]] for r in agent)
    # D only on hint games, each with recorded ground truth.
    d = [p for p in agent if p["variant"] == "D"]
    assert d and all(p["game_id"] in manifest["hint_game_ids"] for p in d)
    assert all(p["hint"]["ground_truth"]["verified_false"] for p in d)
    # Memory: loaded notes always predate as_of; first B prediction sees none.
    b = [p for p in agent if p["variant"] == "B"]
    assert b[0]["loaded_memory"] == []
    for p in b:
        for n in p["loaded_memory"]:
            assert n["created_at"] < p["as_of"]
    assert any(p["loaded_memory"] for p in b[1:])
    # Anonymized: no real team names in any task text.
    for p in agent:
        task = p["trajectory"][0]["text"]
        assert "BOS" not in task and "Celtics" not in task
    # Report builds from the artifacts alone.
    report = build_report(tmp_path, tmp_path / "REPORT.md", run_dirs=[out])
    text = report.read_text()
    assert "replay_anonymized" in text and "RQ3" in text and "Limitations" in text


def test_budget_stops_run_cleanly(league_store, tmp_path):
    cfg = ExperimentConfig(**(CFG | {"budget": {"usd": 0.001, "tokens": 10**8}}))
    out = run_replay(cfg, PREREG, league_store, HeuristicFakeLLM(), out_root=tmp_path)
    m = json.loads((out / "manifest.json").read_text())
    assert m["stopped_reason"].startswith("usd budget") and m["usd_spent"] <= 0.001 * 1.5


def test_preregistration_gate(tmp_path):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    cfg = tmp_path / "exp.yaml"
    cfg.write_text("name: x\n")
    with pytest.raises(PreregistrationError):
        check_preregistered(cfg, "h", repo=tmp_path)
    assert check_preregistered(cfg, "h", exploratory=True, repo=tmp_path).exploratory
    git("add", "exp.yaml")
    git("commit", "-qm", "prereg")
    rec = check_preregistered(cfg, "h", repo=tmp_path)
    assert rec.committed and rec.git_commit
    cfg.write_text("name: x\nvariants: [A, E]\n")  # edited after registration
    with pytest.raises(PreregistrationError):
        check_preregistered(cfg, "h", repo=tmp_path)


def test_live_dry_run_predicts_pre_tipoff_then_resolves(league_store, tmp_path):
    cfg = ExperimentConfig(**(CFG | {"mode": "live"}))
    games = league_store.scheduled_between(datetime(2031, 1, 5, tzinfo=UTC),
                                           datetime(2031, 1, 6, tzinfo=UTC))
    now = games[0].tipoff_utc - timedelta(hours=3)
    s = live_predict(cfg, PREREG, league_store, HeuristicFakeLLM(), now, tmp_path,
                     horizon=timedelta(hours=24))
    assert s["agent_predictions"] > 0 and s["baseline_predictions"] > 0
    recs = Ledger(tmp_path / "predictions.jsonl").verify()
    assert all(r["as_of"] < r["tipoff_utc"] for r in recs if r["kind"] in ("agent", "baseline"))
    # Re-running the same job is idempotent.
    s2 = live_predict(cfg, PREREG, league_store, HeuristicFakeLLM(), now, tmp_path,
                      horizon=timedelta(hours=24))
    assert s2["agent_predictions"] == 0 and s2["baseline_predictions"] == 0
    # Resolving before games finish writes nothing; after, everything resolves.
    assert live_resolve(league_store, None, now, tmp_path)["resolved"] == 0
    r = live_resolve(league_store, HeuristicFakeLLM(), now + timedelta(days=2), tmp_path)
    assert r["resolved"] > 0 and r["still_pending"] == 0
