"""A FAKE `claude -p` for offline tests of the claude_code backend.

Mimics the parts of the real CLI the backend relies on:
  * plain mode: reads the whole prompt from stdin, prints one turn of stream-json events;
  * `--input-format stream-json`: reads one JSON user message per line and answers each
    with a turn (assistant/user events ending in a `result` event), like the real CLI;
  * launches the MCP server from --mcp-config, does the handshake and calls tools.

Behaviour per FAKE_CLAUDE_MODE:
  submit (default) | critique | critique_stall | never_submit | silent | usage_limit |
  foreign_tool
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def emit(e: dict) -> None:
    sys.stdout.write(json.dumps(e) + "\n")
    sys.stdout.flush()


class MCP:
    def __init__(self, cfg_path: str) -> None:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        self.name, spec = next(iter(cfg["mcpServers"].items()))
        self.proc = subprocess.Popen([spec["command"], *spec["args"]], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, encoding="utf-8")
        self.rid = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18"})
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                          "method": "notifications/initialized"}) + "\n")
        self.tools = [f"mcp__{self.name}__{t['name']}" for t in self.rpc("tools/list")["tools"]]

    def rpc(self, method, params=None):
        self.rid += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.rid, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())["result"]

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def run_turn(mcp: MCP, calls: list[tuple[str, dict]], counter: list[int]) -> None:
    for tname, targs in calls:
        counter[0] += 1
        tid = f"toolu_{counter[0]}"
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": f"mcp__forecast__{tname}",
             "input": targs}], "stop_reason": "tool_use"}})
        res = mcp.rpc("tools/call", {"name": tname, "arguments": targs})
        emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": res["content"],
             "is_error": res["isError"]}]}})
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}],
                                           "stop_reason": "end_turn"}})
    emit({"type": "result", "subtype": "success", "is_error": False,
          "num_turns": len(calls) + 1, "result": "done", "total_cost_usd": 0.001,
          "usage": {"input_tokens": 1000, "output_tokens": 200,
                    "cache_read_input_tokens": 500, "cache_creation_input_tokens": 100}})


def plan(mode: str, turn: int, team: str) -> list[tuple[str, dict]]:
    form = ("get_team_form", {"team": team, "last_n": 5})

    def sub(p, why):
        return ("submit_forecast", {"prob_home_win": p, "reasoning": why})

    if mode == "submit":
        return [form, sub(0.61, "fake")] if turn == 0 else []
    if mode == "critique":
        return [form, sub(0.7, "draft"), sub(0.6, "final")] if turn == 0 else []
    if mode == "critique_stall":  # stops after the draft; finalizes only when nudged
        return [form, sub(0.7, "draft")] if turn == 0 else [sub(0.65, "final after nudge")]
    if mode == "never_submit":
        return [form] * 30 if turn == 0 else []
    if mode == "silent":  # never calls submit, even when nudged
        return [form] if turn == 0 else []
    return []


def main() -> None:
    args = sys.argv[1:]
    mode = os.environ.get("FAKE_CLAUDE_MODE", "submit")
    api_source = "ANTHROPIC_API_KEY" if os.environ.get("ANTHROPIC_API_KEY") else "none"
    model = args[args.index("--model") + 1]
    streaming = "--input-format" in args
    mcp = MCP(args[args.index("--mcp-config") + 1]) if "--mcp-config" in args else None
    tools = list(mcp.tools) if mcp else []
    if mode == "foreign_tool":
        tools.append("Bash")
    emit({"type": "system", "subtype": "init", "apiKeySource": api_source, "model": model,
          "tools": tools, "mcp_servers": [{"name": "forecast", "status": "connected"}]
          if mcp else [], "claude_code_version": "fake"})
    if mode == "usage_limit":
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
              "result": "Claude AI usage limit reached|1234", "usage": {}})
        return
    if mcp is None:  # plain completion (memory note)
        sys.stdin.read()
        emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
              "result": "Note: margins mattered.", "usage": {"input_tokens": 50,
                                                              "output_tokens": 8}})
        return
    messages = (json.loads(x)["message"]["content"] for x in sys.stdin if x.strip()) \
        if streaming else iter([sys.stdin.read()])
    counter = [0]
    team = None
    for turn, text in enumerate(messages):
        if team is None:
            team = text.split("tools: ")[1].split(",")[0]
        run_turn(mcp, plan(mode, turn, team), counter)
    mcp.close()


if __name__ == "__main__":
    main()
