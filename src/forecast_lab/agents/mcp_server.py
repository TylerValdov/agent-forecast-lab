"""Minimal stdio MCP server exposing the forecasting tools for ONE (game, as_of).

Used by the `claude_code` backend: Claude Code launches this process, lists its tools and
calls them. Every call still goes through `NBAToolbox` -> `DataStore`, so the as_of guard
and the Masker apply exactly as in the API backend.

Implements the small subset of MCP (JSON-RPC 2.0 over newline-delimited stdio) that tool
use needs: initialize, tools/list, tools/call, ping. Every call is appended to a JSONL log
that the backend reads back (submissions, step counting, trajectory).

Run: python -m forecast_lab.agents.mcp_server <context.json>
"""

from __future__ import annotations

import json
import pickle
import sys
from datetime import datetime
from pathlib import Path

from forecast_lab.agents import prompts
from forecast_lab.data.masker import Masker
from forecast_lab.data.schema import ScheduledGame
from forecast_lab.tools.nba_tools import NBAToolbox, parse_forecast

SERVER_NAME = "forecast"


class ForecastSession:
    """Tool-call state machine (separate from I/O so it can be unit-tested)."""

    def __init__(self, toolbox: NBAToolbox, critique: bool, max_tool_calls: int,
                 prompt_version: str, log_path: Path | None) -> None:
        self.toolbox = toolbox
        self.critique = critique
        self.max_calls = max_tool_calls
        self.prompt_version = prompt_version
        self.log_path = log_path
        self.n_calls = 0
        self.draft = None
        self.final = None

    def _log(self, rec: dict) -> None:
        if self.log_path is not None:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        self.n_calls += 1
        event = "tool"
        if self.final is not None:
            out, err = json.dumps({"error": "Forecast already submitted; stop now."}), True
        elif self.n_calls > self.max_calls:
            out, err, event = json.dumps({"error": "Step limit reached."}), True, "step_limit"
        elif name == "submit_forecast":
            fc, msg = parse_forecast(args)
            if fc is None:
                out, err, event = json.dumps({"error": msg}), True, "invalid_submission"
            elif self.critique and self.draft is None:
                self.draft = fc
                out = prompts.load("critique", self.prompt_version).format(
                    draft_prob=fc.prob_home_win)
                err, event = False, "draft"
            else:
                self.final = fc
                out, err, event = json.dumps({"status": "recorded"}), False, "final"
        else:
            out, err = self.toolbox.execute(name, args)
        self._log({"event": event, "n": self.n_calls, "name": name, "input": args,
                   "output": out, "is_error": err,
                   **({"prob": args.get("prob_home_win"), "reasoning": args.get("reasoning")}
                      if event in ("draft", "final") else {})})
        return out, err

    def tool_list(self) -> list[dict]:
        return [{"name": s["name"], "description": s["description"],
                 "inputSchema": s["input_schema"]} for s in self.toolbox.specs()]


def handle(session: ForecastSession, msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification (e.g. notifications/initialized): no reply
        return None
    if method == "initialize":
        version = msg.get("params", {}).get("protocolVersion", "2025-06-18")
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": SERVER_NAME, "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": session.tool_list()}
    elif method == "tools/call":
        p = msg.get("params", {})
        out, err = session.call(p.get("name", ""), p.get("arguments") or {})
        result = {"content": [{"type": "text", "text": out}], "isError": err}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def session_from_context(ctx_path: Path) -> ForecastSession:
    ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
    with open(ctx["store_pickle"], "rb") as f:
        store = pickle.load(f)
    if store.snapshot_hash != ctx["snapshot_hash"]:
        raise RuntimeError("store pickle does not match the run's data snapshot")
    game = ScheduledGame(**ctx["game"])
    masker = Masker(ctx["masker_salt"], enabled=ctx["masker_enabled"])
    toolbox = NBAToolbox(store, game, datetime.fromisoformat(ctx["as_of"]), masker,
                         injuries_enabled=ctx["injuries_enabled"])
    return ForecastSession(toolbox, ctx["critique"], ctx["max_tool_calls"],
                           ctx["prompt_version"], Path(ctx["log_path"]))


def main(argv: list[str]) -> None:
    session = session_from_context(Path(argv[1]))
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        try:
            reply = handle(session, json.loads(raw))
        except Exception as e:  # never crash the session; report as JSON-RPC error
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}
        if reply is not None:
            out.write(json.dumps(reply).encode("utf-8") + b"\n")
            out.flush()


if __name__ == "__main__":
    main(sys.argv)
