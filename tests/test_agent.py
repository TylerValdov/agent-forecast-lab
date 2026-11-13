"""Agent loop, memory time-gating, hints, ledger, budget. (Synthetic data, fake LLMs.)"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from forecast_lab.agents.agent import AgentConfig, run_agent
from forecast_lab.agents.fake import ScriptedLLM, response, text_block, tool_use
from forecast_lab.agents.hints import make_hint, select_hint_games, verify_false
from forecast_lab.agents.memory import MemoryStore, Note
from forecast_lab.data.masker import Masker
from forecast_lab.ledger.jsonl import Ledger, LedgerCorrupted
from forecast_lab.runner.budget import BudgetGuard
from forecast_lab.tools.nba_tools import NBAToolbox


def toolbox(store, gid="G3"):
    g = store.scheduled(gid)
    return NBAToolbox(store, g, g.tipoff_utc - timedelta(hours=1), Masker("s", False), False)


def submit(p, why="because"):
    return response([tool_use("submit_forecast", {"prob_home_win": p, "reasoning": why})])


def test_happy_path_logs_full_trajectory(tiny_store):
    llm = ScriptedLLM([
        response([tool_use("get_team_form", {"team": "BOS", "last_n": 5}),
                  tool_use("get_rest_days", {"team": "MIA"})]),
        submit(0.62),
    ])
    res = run_agent(llm, toolbox(tiny_store), "task", AgentConfig())
    assert res.prob == 0.62 and res.failure is None and res.steps == 2
    kinds = [t["type"] for t in res.trajectory]
    assert kinds.count("tool_result") == 3 and kinds.count("assistant") == 2  # incl. submit
    # Both tool results go back in ONE user message, and history is append-only.
    second = llm.requests[1]["messages"]
    assert second[0] == llm.requests[0]["messages"][0]
    assert [b["type"] for b in second[-1]["content"]] == ["tool_result", "tool_result"]
    assert res.cost_usd > 0


def test_out_of_range_forecast_rejected_then_retried(tiny_store):
    llm = ScriptedLLM([submit(0.99), submit(0.9)])
    res = run_agent(llm, toolbox(tiny_store), "task", AgentConfig())
    assert res.prob == 0.9 and res.invalid_submissions == 1


def test_never_submitting_is_a_recorded_failure_not_a_default(tiny_store):
    llm = ScriptedLLM([response([text_block("I think 60%.")])] * 10)
    res = run_agent(llm, toolbox(tiny_store), "task", AgentConfig(max_steps=3))
    assert res.prob is None and res.failure == "step_limit"


def test_refusal_and_token_cap(tiny_store):
    res = run_agent(ScriptedLLM([response([], stop="refusal")]), toolbox(tiny_store), "t",
                    AgentConfig())
    assert res.failure == "refusal"
    loop = [response([tool_use("get_rest_days", {"team": "BOS"})])] * 10
    res = run_agent(ScriptedLLM(loop), toolbox(tiny_store), "t",
                    AgentConfig(max_total_tokens=250))
    assert res.failure == "token_limit"


def test_self_critique_logs_draft_and_final(tiny_store):
    llm = ScriptedLLM([submit(0.8, "draft"), submit(0.7, "final")])
    res = run_agent(llm, toolbox(tiny_store), "task", AgentConfig(), critique=True)
    assert (res.draft_prob, res.prob) == (0.8, 0.7)
    critique_msg = llm.requests[1]["messages"][-1]["content"][0]["content"]
    assert "critique" in critique_msg.lower()


def test_tool_errors_are_returned_not_raised(tiny_store):
    llm = ScriptedLLM([response([tool_use("get_team_form", {"team": "XXX", "last_n": 5})]),
                       submit(0.5)])
    res = run_agent(llm, toolbox(tiny_store), "task", AgentConfig())
    assert res.tool_errors == 1 and res.prob == 0.5


def test_memory_is_time_gated(tmp_path):
    t0 = datetime(2030, 1, 1, tzinfo=UTC)
    mem = MemoryStore(tmp_path / "m.jsonl")
    for i in range(5):
        mem.add(Note(f"n{i}", t0 + timedelta(hours=i), f"G{i}", f"note {i}"))
    assert [n.note_id for n in mem.load(t0 + timedelta(hours=2), k=10)] == ["n0", "n1"]
    assert mem.load(t0, k=10) == []  # strict: a note created exactly at as_of is hidden
    assert [n.note_id for n in mem.load(t0 + timedelta(days=1), k=2)] == ["n3", "n4"]
    assert len(MemoryStore(tmp_path / "m.jsonl")) == 5  # persisted + reloadable


def test_hints_are_verifiably_false(league_store):
    games = [r.to_scheduled() for r in league_store.results(datetime(2031, 6, 1, tzinfo=UTC))]
    games = games[60:200]
    made = 0
    for g in games:
        as_of = g.tipoff_utc - timedelta(hours=1)
        h = make_hint(league_store, g, as_of, Masker("s", False), seed=3)
        if h:
            made += 1
            assert verify_false(league_store, g, as_of, h)
            assert h.ground_truth["verified_false"] and h.direction in (-1, 1)
    assert made > 0.8 * len(games)


def test_hint_selection_is_seeded():
    ids = [f"G{i}" for i in range(100)]
    a, b = select_hint_games(ids, 0.2, 7), select_hint_games(list(reversed(ids)), 0.2, 7)
    assert a == b and len(a) == 20
    assert select_hint_games(ids, 0.2, 8) != a


def test_ledger_is_append_only_and_tamper_evident(tmp_path):
    p = tmp_path / "l.jsonl"
    led = Ledger(p)
    for i in range(3):
        led.append({"game_id": f"G{i}", "prob": 0.5 + i / 10,
                    "as_of": datetime(2030, 1, 1, tzinfo=UTC)})
    assert len(Ledger(p).verify()) == 3
    lines = p.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["prob"] = 0.99
    lines[1] = json.dumps(rec)
    p.write_text("\n".join(lines) + "\n")
    with pytest.raises(LedgerCorrupted):
        Ledger(p)


def test_budget_guard_stops_cleanly():
    b = BudgetGuard(usd_cap=1.0, token_cap=10**9)
    n = 0
    while b.can_afford_next():
        b.charge(0.3, 100)
        n += 1
    assert n == 2 and "usd" in b.stopped_reason and b.usd_spent <= 1.0
