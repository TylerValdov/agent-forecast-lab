"""The claude_code (subscription) backend, tested offline with a fake CLI.
(Synthetic data only.)"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from forecast_lab.agents.agent import AgentConfig
from forecast_lab.agents.claude_code import (
    ClaudeCodeLLM,
    IsolationError,
    UsageLimitReached,
    check_isolation,
)
from forecast_lab.agents.fake import HeuristicFakeLLM
from forecast_lab.agents.mcp_server import ForecastSession, handle
from forecast_lab.data.masker import Masker
from forecast_lab.ledger.jsonl import Ledger
from forecast_lab.runner.config import ExperimentConfig
from forecast_lab.runner.prereg import PreregRecord
from forecast_lab.runner.replay import run_replay
from forecast_lab.tools.nba_tools import NBAToolbox

FAKE = [sys.executable, str(Path(__file__).with_name("fake_claude.py"))]


def toolbox(store, masked=False):
    g = store.scheduled("G3")
    return NBAToolbox(store, g, g.tipoff_utc - timedelta(hours=1),
                      Masker("salt", enabled=masked), injuries_enabled=False)


def test_mcp_session_protocol_and_guard(tiny_store, tmp_path):
    s = ForecastSession(toolbox(tiny_store), critique=False, max_tool_calls=3,
                        prompt_version="v1", log_path=tmp_path / "log.jsonl")
    init = handle(s, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert handle(s, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in handle(s, {"jsonrpc": "2.0", "id": 2,
                                           "method": "tools/list"})["result"]["tools"]}
    assert names == {"get_team_form", "get_rest_days", "get_head_to_head", "submit_forecast"}
    r = handle(s, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "get_team_form", "arguments": {"team": "BOS", "last_n": 20}}})["result"]
    assert "95-130" not in r["content"][0]["text"]  # the game's own score never leaks
    bad = handle(s, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "submit_forecast", "arguments": {"prob_home_win": 2, "reasoning": "x"}}})
    assert bad["result"]["isError"]
    ok = handle(s, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
        "name": "submit_forecast", "arguments": {"prob_home_win": 0.6, "reasoning": "x"}}})
    assert not ok["result"]["isError"] and s.final.prob_home_win == 0.6
    events = [json.loads(x)["event"] for x in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert events == ["tool", "invalid_submission", "final"]


def test_isolation_checks():
    check_isolation({"apiKeySource": "none", "tools": ["mcp__forecast__x"],
                     "mcp_servers": [{"name": "forecast"}]}, expect_tools=True)
    with pytest.raises(IsolationError):
        check_isolation({"apiKeySource": "ANTHROPIC_API_KEY", "tools": []}, False)
    with pytest.raises(IsolationError):
        check_isolation({"apiKeySource": "none", "tools": ["mcp__forecast__x", "Bash"]}, True)
    with pytest.raises(IsolationError):
        check_isolation({"apiKeySource": "none", "tools": [],
                         "mcp_servers": [{"name": "forecast", "status": "failed"}]}, True)


def test_backend_end_to_end_with_fake_cli(tiny_store, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "submit")
    llm = ClaudeCodeLLM("claude-haiku-5-5", command=FAKE)
    task = "Game: MIA (away) at BOS (home).\nUse these exact team identifiers with the " \
           "tools: BOS, MIA.\nWhat is the probability?"
    res = llm.run_forecast(toolbox(tiny_store), task, AgentConfig(), critique=False)
    assert res.prob == 0.61 and res.failure is None
    assert res.usage.input_tokens == 1000 and res.cost_usd > 0
    kinds = [t["type"] for t in res.trajectory]
    assert kinds[0] == "task" and "tool_result" in kinds and kinds[-1] == "backend"
    assert llm.last_init["apiKeySource"] == "none"  # API key was stripped from the env


def test_backend_critique_and_failures(tiny_store, monkeypatch):
    llm = ClaudeCodeLLM("claude-haiku-5-5", command=FAKE)
    task = "tools: BOS, MIA."
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "critique")
    res = llm.run_forecast(toolbox(tiny_store), task, AgentConfig(), critique=True)
    assert (res.draft_prob, res.prob) == (0.7, 0.6)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "never_submit")
    res = llm.run_forecast(toolbox(tiny_store), task, AgentConfig(max_steps=3), critique=False)
    assert res.prob is None and res.failure == "step_limit"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "foreign_tool")
    with pytest.raises(IsolationError):
        llm.run_forecast(toolbox(tiny_store), task, AgentConfig(), critique=False)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "usage_limit")
    with pytest.raises(UsageLimitReached):
        llm.run_forecast(toolbox(tiny_store), task, AgentConfig(), critique=False)


def test_backend_masks_in_anonymized_mode(tiny_store, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "submit")
    tb = toolbox(tiny_store, masked=True)
    home, away = tb.label("BOS"), tb.label("MIA")
    llm = ClaudeCodeLLM("claude-haiku-5-5", command=FAKE)
    res = llm.run_forecast(tb, f"tools: {home}, {away}.", AgentConfig(), critique=False)
    outputs = " ".join(t["output"] for t in res.trajectory if t["type"] == "tool_result")
    assert res.prob == 0.61 and home in outputs
    assert tb.masker.leaked_terms(outputs) == []


class LimitedFake(HeuristicFakeLLM):
    """Fake LLM that hits a 'usage limit' after N calls (for resume tests)."""

    backend = "api"

    def __init__(self, limit):
        self.calls, self.limit = 0, limit

    def create(self, **kw):
        self.calls += 1
        if self.calls > self.limit:
            raise UsageLimitReached("limit")
        return super().create(**kw)


def test_usage_limit_stops_cleanly_and_resume_completes(league_store, tmp_path):
    cfg = ExperimentConfig(
        name="r", mode="replay_anonymized", model="claude-haiku-5-5",
        variants=["A", "B"], games={"start": "2030-12-15", "end": "2031-01-01",
                                    "sample": 12, "sample_seed": 1},
        budget={"usd": 5, "tokens": 10**8})
    prereg = PreregRecord("r.yaml", "a" * 64, None, False, True)
    out = run_replay(cfg, prereg, league_store, LimitedFake(limit=25), out_root=tmp_path)
    m = json.loads((out / "manifest.json").read_text())
    assert m["stopped_reason"].startswith("subscription usage limit")
    n1 = m["n_predictions"]
    out2 = run_replay(cfg, prereg, league_store, LimitedFake(limit=10**6), out_root=tmp_path,
                      resume_dir=out)
    assert out2 == out
    m2 = json.loads((out / "manifest.json").read_text())
    recs = Ledger(out / "predictions.jsonl").verify()
    agent = [r for r in recs if r["kind"] == "agent"]
    keys = [(r["game_id"], r["variant"]) for r in agent]
    assert m2["stopped_reason"] == "completed" and len(m2["resumes"]) == 1
    assert len(keys) == len(set(keys)) == 24 and m2["n_predictions"] == 24 > n1
    assert len(Ledger(out / "outcomes.jsonl").verify()) == 12
    # Predict-before-reveal still holds across the resume boundary.
    written = {r["game_id"]: r["written_at"] for r in Ledger(out / "outcomes.jsonl").verify()}
    assert all(r["timestamp"] <= written[r["game_id"]] for r in agent)


def test_resume_refuses_changed_config(league_store, tmp_path):
    from forecast_lab.runner.replay import ResumeError

    cfg = ExperimentConfig(name="r", mode="replay_anonymized", model="claude-haiku-5-5",
                           variants=["A"], games={"start": "2030-12-15", "end": "2030-12-17"},
                           budget={"usd": 5, "tokens": 10**8})
    out = run_replay(cfg, PreregRecord("r", "a" * 64, None, False, True), league_store,
                     HeuristicFakeLLM(), out_root=tmp_path)
    with pytest.raises(ResumeError):
        run_replay(cfg, PreregRecord("r", "b" * 64, None, False, True), league_store,
                   HeuristicFakeLLM(), out_root=tmp_path, resume_dir=out)
