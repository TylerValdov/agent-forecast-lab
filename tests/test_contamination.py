"""Contamination audit (RQ5): real-name variants, the recall guardrail, the recall probe.
(Synthetic data, fake LLMs.)"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from forecast_lab.agents.agent import AgentConfig
from forecast_lab.agents.fake import HeuristicFakeLLM, ScriptedLLM, response, text_block
from forecast_lab.data.masker import Masker
from forecast_lab.ledger.jsonl import Ledger
from forecast_lab.report.build import build_report
from forecast_lab.runner.config import ExperimentConfig
from forecast_lab.runner.predict import RunContext, predict
from forecast_lab.runner.prereg import PreregRecord
from forecast_lab.runner.probe import ProbeItem, ask, parse_answer, probe_items, score
from forecast_lab.runner.replay import run_replay


def ctx(masked=True):
    return RunContext("r", "h" * 64, "replay_anonymized", Masker("s", enabled=masked),
                      AgentConfig(), 60)


def test_real_name_variants_unmask_and_guardrail_only_on_g(tiny_store):
    g = tiny_store.scheduled("G3")
    as_of = g.tipoff_utc - timedelta(hours=1)
    seen = {}
    for v in ["A", "R", "G"]:
        llm = HeuristicFakeLLM()
        calls = []
        orig = llm.create

        def spy(*, system, messages, tools, max_tokens, _orig=orig, _calls=calls):
            _calls.append((system, messages[0]["content"]))
            return _orig(system=system, messages=messages, tools=tools, max_tokens=max_tokens)

        llm.create = spy
        rec = predict(llm, tiny_store, g, as_of, v, ctx())
        seen[v] = (calls[0], rec)
    (sys_a, task_a), rec_a = seen["A"]
    (sys_r, task_r), rec_r = seen["R"]
    (sys_g, task_g), rec_g = seen["G"]
    assert "BOS" not in task_a and rec_a["mode"] == "replay_anonymized"
    assert "BOS" in task_r and "2030-01-11" in task_r and rec_r["mode"] == "replay_raw"
    assert task_g == task_r  # G differs from R only in the system prompt
    assert "Do not use anything you may remember" in sys_g
    assert "remember" not in sys_r and sys_a == sys_r
    assert rec_r["prob"] is not None and rec_g["prob"] is not None


def test_probe_parse_and_score(tiny_store):
    g = tiny_store.reveal("G3", datetime(2100, 1, 1, tzinfo=UTC))  # BOS 95 - 130 MIA
    assert parse_answer('Sure: {"home_pts": 95, "away_pts": 130, "remembered": true}') == \
        {"home_pts": 95, "away_pts": 130, "remembered": True}
    assert parse_answer("no idea") is None
    s = score({"home_pts": 95, "away_pts": 130, "remembered": True}, g)
    assert s["exact"] and s["winner_correct"] and s["margin_abs_err"] == 0
    s = score({"home_pts": 110, "away_pts": 100, "remembered": False}, g)
    assert not s["exact"] and not s["winner_correct"] and s["margin_abs_err"] == 45
    assert score(None, g) == {"parsed": False}


def test_probe_question_uses_real_names_and_no_tools(tiny_store):
    g = tiny_store.reveal("G3", datetime(2100, 1, 1, tzinfo=UTC))
    llm = ScriptedLLM([response([text_block('{"home_pts": 100, "away_pts": 99, '
                                            '"remembered": false}')])])
    rec = ask(llm, ProbeItem(g, "pre"))
    req = llm.requests[0]
    assert req["tools"] == [] and "Miami Heat (away) at Boston Celtics (home)" in \
        req["messages"][0]["content"]
    assert rec["parsed"] and not rec["exact"] and rec["group"] == "pre"


def test_probe_items_split_pre_and_post(league_store):
    finished = league_store.results(datetime(2100, 1, 1, tzinfo=UTC))
    items = probe_items(finished[:50], league_store, n_pre=10, n_post=5,
                        post_after=date(2031, 2, 1), seed=1, now=datetime(2031, 6, 1, tzinfo=UTC))
    pre = [i for i in items if i.group == "pre"]
    post = [i for i in items if i.group == "post"]
    assert len(pre) == 10 and len(post) == 5
    assert all(i.game.tipoff_utc >= datetime(2031, 2, 1, tzinfo=UTC) for i in post)


def test_contamination_run_end_to_end(league_store, tmp_path):
    cfg = ExperimentConfig(
        name="c", mode="replay_anonymized", model="claude-haiku-5-5", variants=["A", "R", "G"],
        games={"start": "2030-12-15", "end": "2031-01-10", "sample": 12, "sample_seed": 3},
        recall_probe={"n_pre": 6, "n_post": 4, "post_after": "2031-02-01", "seed": 1},
        budget={"usd": 5, "tokens": 10**8},
        evaluation={"min_n": 5, "bootstrap_resamples": 200, "comparisons": [
            {"a": "R", "b": "A", "rq": "RQ5a"}, {"a": "G", "b": "R", "rq": "RQ5b"}]})
    out = run_replay(cfg, PreregRecord("c", "c" * 64, None, False, True), league_store,
                     HeuristicFakeLLM(), out_root=tmp_path, post_store=league_store,
                     clock=datetime(2032, 1, 1, tzinfo=UTC))
    recs = Ledger(out / "predictions.jsonl").verify()
    agent = [r for r in recs if r["kind"] == "agent"]
    probes = [r for r in recs if r["kind"] == "probe"]
    assert {r["variant"] for r in agent} == {"A", "R", "G"} and len(agent) == 36
    assert sorted(r["group"] for r in probes) == ["post"] * 4 + ["pre"] * 6
    # Every probe question is about a finished game and was asked after all forecasts.
    assert max(r["timestamp"] for r in agent) <= min(r["timestamp"] for r in probes)
    text = Path(build_report(tmp_path, tmp_path / "R.md", run_dirs=[out])).read_text()
    assert "RQ5: contamination audit" in text and "Recall probe" in text
    # Resuming a finished run re-asks nothing.
    run_replay(cfg, PreregRecord("c", "c" * 64, None, False, True), league_store,
               HeuristicFakeLLM(), out_root=tmp_path, resume_dir=out, post_store=league_store,
               clock=datetime(2032, 1, 1, tzinfo=UTC))
    assert len([r for r in Ledger(out / "predictions.jsonl").verify()
                if r["kind"] == "probe"]) == 10
