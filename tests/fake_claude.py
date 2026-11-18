"""A FAKE `claude -p` for offline tests of the claude_code backend.

Mimics the parts of the real CLI the backend relies on: reads the prompt from stdin,
launches the MCP server from --mcp-config, performs the MCP handshake, calls tools, and
prints stream-json events (init / assistant / user / result).

Behaviour is selected with the FAKE_CLAUDE_MODE environment variable:
  submit (default) | critique | never_submit | usage_limit | foreign_tool
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def emit(e: dict) -> None:
    sys.stdout.write(json.dumps(e) + "\n")


def main() -> None:
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    mode = os.environ.get("FAKE_CLAUDE_MODE", "submit")
    api_source = "ANTHROPIC_API_KEY" if os.environ.get("ANTHROPIC_API_KEY") else "none"
    model = args[args.index("--model") + 1]
    tools: list[str] = []
    proc = None
    if "--mcp-config" in args:
        with open(args[args.index("--mcp-config") + 1], encoding="utf-8") as f:
            cfg = json.load(f)
        name, spec = next(iter(cfg["mcpServers"].items()))
        proc = subprocess.Popen([spec["command"], *spec["args"]], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, encoding="utf-8")
        rid = 0

        def rpc(method, params=None):
            nonlocal rid
            rid += 1
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                         "params": params or {}}) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())["result"]

        rpc("initialize", {"protocolVersion": "2025-06-18"})
        proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                     "method": "notifications/initialized"}) + "\n")
        tools = [f"mcp__{name}__{t['name']}" for t in rpc("tools/list")["tools"]]
    if mode == "foreign_tool":
        tools.append("Bash")
    emit({"type": "system", "subtype": "init", "apiKeySource": api_source, "model": model,
          "tools": tools, "mcp_servers": [{"name": "forecast", "status": "connected"}]
          if proc else [], "claude_code_version": "fake"})
    if mode == "usage_limit":
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
              "result": "Claude AI usage limit reached|1234", "usage": {}})
        return
    if proc is None:  # plain completion (memory note)
        emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
              "result": "Note: margins mattered.", "usage": {"input_tokens": 50,
                                                              "output_tokens": 8}})
        return

    team = prompt.split("tools: ")[1].split(",")[0]
    calls = [("get_team_form", {"team": team, "last_n": 5})]
    if mode == "submit":
        calls.append(("submit_forecast", {"prob_home_win": 0.61, "reasoning": "fake"}))
    elif mode == "critique":
        calls += [("submit_forecast", {"prob_home_win": 0.7, "reasoning": "draft"}),
                  ("submit_forecast", {"prob_home_win": 0.6, "reasoning": "final"})]
    elif mode == "never_submit":
        calls *= 30
    n = 0
    for tname, targs in calls:
        n += 1
        tid = f"toolu_{n}"
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": f"mcp__forecast__{tname}",
             "input": targs}], "stop_reason": "tool_use"}})
        res = rpc("tools/call", {"name": tname, "arguments": targs})
        emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": res["content"],
             "is_error": res["isError"]}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": n + 1,
          "result": "done", "total_cost_usd": 0.001,
          "usage": {"input_tokens": 1000, "output_tokens": 200,
                    "cache_read_input_tokens": 500, "cache_creation_input_tokens": 100}})
    proc.stdin.close()
    proc.wait(timeout=10)


if __name__ == "__main__":
    main()
