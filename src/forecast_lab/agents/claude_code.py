"""`claude_code` backend: run the forecasting agent through the Claude Code CLI in headless
mode (`claude -p`), which bills the user's Claude subscription instead of an API key.

How one prediction runs:
  * our system prompt replaces Claude Code's (`--system-prompt-file`);
  * all built-in tools are disabled (`--tools ""`), user/project settings, plugins and
    hooks are not loaded (`--setting-sources ""`), and the only tools are ours, served by
    `forecast_lab.agents.mcp_server` bound to this (game, as_of);
  * ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are removed from the child environment, and the
    session's init event must report `apiKeySource == "none"` (subscription login), so a
    run can never silently bill an API account;
  * the init event is checked: if any tool other than ours is visible, the run aborts.

Known, documented differences from the API backend (docs/DECISIONS.md D28):
  * Claude Code always adds ~1.1k tokens of its own context, including today's real date,
    the model's knowledge cutoff and an environment block. Harmless in `live` mode; a
    confound in replay modes, so replay results from this backend are labelled and kept
    separate from API results.
  * Step limit is enforced as a cap on tool calls inside the MCP server.
  * `cost_usd` is the API-equivalent list price of the tokens used (no money is charged);
    the budget guard uses it as a usage cap.
"""

from __future__ import annotations

import json
import os
import pickle
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from forecast_lab.agents import prompts
from forecast_lab.agents.agent import (
    FAIL_API,
    FAIL_REFUSAL,
    FAIL_STEP_LIMIT,
    NO_SUBMIT_NUDGE,
    AgentConfig,
    AgentResult,
)
from forecast_lab.agents.llm import LLMError, LLMResponse, Usage, cost_usd
from forecast_lab.agents.mcp_server import SERVER_NAME
from forecast_lab.tools.nba_tools import NBAToolbox

FAIL_TIMEOUT = "timeout"
FAIL_NO_SUBMIT = "no_submit"
FAIL_USAGE_LIMIT = "usage_limit"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
MAX_NUDGES = 2  # extra user turns when a turn ends without a final forecast
_SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class UsageLimitReached(RuntimeError):
    """The subscription's usage limit was hit; the runner stops cleanly and can resume."""


class IsolationError(RuntimeError):
    """Claude Code exposed something other than our tools, or used an API key."""


def _usage(result: dict) -> Usage:
    u = result.get("usage") or {}
    return Usage(u.get("input_tokens", 0), u.get("output_tokens", 0),
                 u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))


def check_isolation(init: dict, expect_tools: bool) -> None:
    if init.get("apiKeySource") not in ("none", None):
        raise IsolationError(f"claude used an API key ({init.get('apiKeySource')}); "
                             "the claude_code backend must run on the subscription login")
    tools = init.get("tools") or []
    foreign = [t for t in tools if not t.startswith(TOOL_PREFIX)]
    if foreign:
        raise IsolationError(f"unexpected tools visible to the agent: {foreign}")
    servers = {s.get("name") for s in init.get("mcp_servers") or []}
    if servers - {SERVER_NAME}:
        raise IsolationError(f"unexpected MCP servers: {servers - {SERVER_NAME}}")
    if expect_tools and not tools:
        status = [s.get("status") for s in init.get("mcp_servers") or []]
        raise IsolationError(f"forecast tools not available (MCP server status {status})")


def _is_usage_limit(result: dict) -> bool:
    text = f"{result.get('result', '')} {result.get('subtype', '')}".lower()
    return result.get("is_error") and ("usage limit" in text or "limit reached" in text
                                       or "rate limit" in text)


def events_to_trajectory(task: str, events: list[dict]) -> list[dict]:
    """Stream-json events -> the same trajectory shape the API backend logs."""
    traj: list[dict] = [{"type": "task", "text": task}]
    names: dict[str, str] = {}
    inputs: dict[str, dict] = {}
    step = 0
    for e in events:
        msg = e.get("message") or {}
        if e.get("type") == "assistant":
            step += 1
            content = msg.get("content") or []
            for b in content:
                if b.get("type") == "tool_use":
                    names[b["id"]] = b["name"].removeprefix(TOOL_PREFIX)
                    inputs[b["id"]] = b.get("input")
            traj.append({"type": "assistant", "step": step, "content": content,
                         "stop_reason": msg.get("stop_reason")})
        elif e.get("type") == "nudge":
            traj.append({"type": "nudge", "text": e["text"]})
        elif e.get("type") == "user":
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out = b.get("content")
                    if isinstance(out, list):
                        out = " ".join(x.get("text", "") for x in out if isinstance(x, dict))
                    traj.append({"type": "tool_result",
                                 "name": names.get(b.get("tool_use_id"), "?"),
                                 "input": inputs.get(b.get("tool_use_id")),
                                 "output": out, "is_error": bool(b.get("is_error"))})
    return traj


class ClaudeCodeLLM:
    backend = "claude_code"

    def __init__(self, model: str, effort: str = "low", timeout_s: float = 600.0,
                 command: list[str] | None = None) -> None:
        """`command` overrides how the CLI is launched (tests use a fake CLI)."""
        self.model = model
        self.effort = effort
        self.timeout_s = timeout_s
        if command is None:
            exe = shutil.which("claude")
            if not exe:
                raise RuntimeError("Claude Code CLI ('claude') not found on PATH")
            command = [exe]
        self.command = command
        self._tmp = Path(tempfile.mkdtemp(prefix="forecast_cc_"))
        self._store_pickles: dict[str, Path] = {}
        self.last_init: dict | None = None

    # ------------------------------------------------------------------ internals
    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _run(self, system: str, prompt: str, mcp_config: dict | None,
             workdir: Path) -> tuple[list[dict], float]:
        sys_file = workdir / "system.md"
        sys_file.write_text(system, encoding="utf-8")
        cmd = [*self.command, "-p", "--output-format", "stream-json", "--verbose",
               "--model", self.model, "--effort", self.effort,
               "--tools", "", "--setting-sources", "", "--strict-mcp-config",
               "--no-session-persistence", "--system-prompt-file", str(sys_file)]
        if mcp_config is not None:
            cfg_file = workdir / "mcp.json"
            cfg_file.write_text(json.dumps(mcp_config), encoding="utf-8")
            cmd += ["--mcp-config", str(cfg_file), "--allowedTools", f"mcp__{SERVER_NAME}",
                    "--permission-mode", "dontAsk"]
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  encoding="utf-8", cwd=workdir, env=self._env(),
                                  timeout=self.timeout_s)
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(f"claude -p timed out after {self.timeout_s}s") from e
        events = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if not events:
            raise LLMError(f"claude -p produced no events (exit {proc.returncode}): "
                           f"{proc.stderr[-500:]}")
        return events, time.perf_counter() - t0

    def _store_pickle(self, toolbox: NBAToolbox) -> Path:
        h = toolbox.store.snapshot_hash
        if h not in self._store_pickles:
            p = self._tmp / f"store_{h}.pkl"
            with p.open("wb") as f:
                pickle.dump(toolbox.store, f, protocol=pickle.HIGHEST_PROTOCOL)
            self._store_pickles[h] = p
        return self._store_pickles[h]

    @staticmethod
    def _split(events: list[dict]) -> tuple[dict, dict]:
        init = next((e for e in events if e.get("type") == "system"
                     and e.get("subtype") == "init"), {})
        result = next((e for e in reversed(events) if e.get("type") == "result"), {})
        return init, result

    # ------------------------------------------------------------------- public
    def create(self, *, system, messages, tools, max_tokens) -> LLMResponse:
        """Single-turn, tool-free completion (used for memory notes)."""
        if tools or len(messages) != 1 or not isinstance(messages[0]["content"], str):
            raise NotImplementedError("claude_code.create supports one plain user message")
        workdir = Path(tempfile.mkdtemp(dir=self._tmp))
        try:
            events, latency = self._run(system, messages[0]["content"], None, workdir)
        except TimeoutError as e:
            raise LLMError(str(e)) from e
        init, result = self._split(events)
        check_isolation(init, expect_tools=False)
        if _is_usage_limit(result):
            raise UsageLimitReached(result.get("result", ""))
        if result.get("is_error"):
            raise LLMError(f"claude -p error: {result.get('subtype')}: {result.get('result')}")
        return LLMResponse([{"type": "text", "text": result.get("result", "")}],
                           result.get("stop_reason") or "end_turn", _usage(result),
                           init.get("model", self.model), latency)

    def _session_cmd(self, sys_file: Path, cfg_file: Path) -> list[str]:
        return [*self.command, "-p", "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose",
                "--model", self.model, "--effort", self.effort,
                "--tools", "", "--setting-sources", "", "--strict-mcp-config",
                "--no-session-persistence", "--system-prompt-file", str(sys_file),
                "--mcp-config", str(cfg_file), "--allowedTools", f"mcp__{SERVER_NAME}",
                "--permission-mode", "dontAsk"]

    def run_forecast(self, toolbox: NBAToolbox, task: str, cfg: AgentConfig,
                     critique: bool, system_extra: str | None = None) -> AgentResult:
        """One multi-turn Claude Code session. If a turn ends without a final forecast, the
        agent gets the same nudge the API backend sends (at most MAX_NUDGES times)."""
        workdir = Path(tempfile.mkdtemp(dir=self._tmp))
        log_path = workdir / "tool_log.jsonl"
        max_calls = 2 * (cfg.max_steps + (cfg.critique_extra_steps if critique else 0))
        ctx = {
            "store_pickle": str(self._store_pickle(toolbox)),
            "snapshot_hash": toolbox.store.snapshot_hash,
            "game": toolbox.game.model_dump(mode="json"),
            "as_of": toolbox.as_of.isoformat(),
            "masker_salt": toolbox.masker.salt, "masker_enabled": toolbox.masker.enabled,
            "injuries_enabled": toolbox.injuries_enabled, "critique": critique,
            "max_tool_calls": max_calls, "prompt_version": cfg.prompt_version,
            "log_path": str(log_path),
        }
        (workdir / "context.json").write_text(json.dumps(ctx), encoding="utf-8")
        cfg_file, sys_file = workdir / "mcp.json", workdir / "system.md"
        cfg_file.write_text(json.dumps({"mcpServers": {SERVER_NAME: {
            "command": sys.executable,
            "args": ["-m", "forecast_lab.agents.mcp_server", str(workdir / "context.json")],
        }}}), encoding="utf-8")
        system = prompts.load("system", cfg.prompt_version)
        if system_extra:
            system = f"{system}\n\n{system_extra}"
        sys_file.write_text(system, encoding="utf-8")

        def read_log() -> list[dict]:
            if not log_path.exists():
                return []
            return [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines()
                    if x.strip()]

        events: list[dict] = []
        results: list[dict] = []
        nudges = 0
        failure: str | None = None
        t0 = time.perf_counter()
        deadline = t0 + self.timeout_s
        session = _Session(self._session_cmd(sys_file, cfg_file), workdir, self._env())
        try:
            session.send(task)
            while True:
                turn = session.until_result(deadline)
                events += turn
                result = turn[-1]
                results.append(result)
                if len(results) == 1:
                    init, _ = self._split(events)
                    check_isolation(init, expect_tools=True)
                    self.last_init = init
                if _is_usage_limit(result):
                    raise UsageLimitReached(result.get("result", ""))
                log = read_log()
                done = (any(r["event"] == "final" for r in log)
                        or any(r["event"] == "step_limit" for r in log)
                        or result.get("is_error") or result.get("stop_reason") == "refusal")
                if done or nudges >= MAX_NUDGES:
                    break
                session.send(NO_SUBMIT_NUDGE)
                nudges += 1
                events.append({"type": "nudge", "text": NO_SUBMIT_NUDGE})
        except TimeoutError:
            failure = FAIL_TIMEOUT
        except LLMError:
            failure = FAIL_API
        finally:
            session.close()
        latency = time.perf_counter() - t0

        init, _ = self._split(events)
        result = results[-1] if results else {}
        usage = Usage()
        for r in results:
            usage += _usage(r)
        log = read_log()
        final = next((r for r in log if r["event"] == "final"), None)
        draft = next((r for r in log if r["event"] == "draft"), None)
        if final is None and failure is None:
            if any(r["event"] == "step_limit" for r in log) \
                    or result.get("subtype") == "error_max_turns":
                failure = FAIL_STEP_LIMIT
            elif result.get("stop_reason") == "refusal":
                failure = FAIL_REFUSAL
            elif result.get("is_error"):
                failure = FAIL_API
            else:
                failure = FAIL_NO_SUBMIT
        trajectory = events_to_trajectory(task, events)
        trajectory.append({"type": "backend", "backend": self.backend,
                           "claude_code_version": init.get("claude_code_version"),
                           "model_reported": init.get("model"),
                           "nudges": nudges,
                           "api_equivalent_cost_reported": sum(
                               r.get("total_cost_usd") or 0 for r in results),
                           "result_subtypes": [r.get("subtype") for r in results],
                           "tool_log": log})
        return AgentResult(
            prob=final["prob"] if final else None,
            reasoning=final["reasoning"] if final else None,
            draft_prob=draft["prob"] if draft else None,
            draft_reasoning=draft["reasoning"] if draft else None,
            failure=failure if final is None else None,
            steps=sum(int(r.get("num_turns") or 0) for r in results),
            usage=usage, cost_usd=cost_usd(self.model, usage), latency_s=latency,
            trajectory=trajectory,
            invalid_submissions=sum(r["event"] == "invalid_submission" for r in log),
            tool_errors=sum(1 for r in log if r["is_error"] and r["event"] == "tool"),
        )


class _Session:
    """A long-lived `claude -p --input-format stream-json` process: send user messages,
    read events until each turn's `result` event, with a wall-clock deadline."""

    def __init__(self, cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     cwd=cwd, env=env)
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._drain_err, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _drain_err(self) -> None:
        for line in self.proc.stderr:
            self.stderr.append(line)

    def send(self, text: str) -> None:
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except OSError as e:
            raise LLMError(f"claude session closed: {e}") from e

    def until_result(self, deadline: float) -> list[dict]:
        events: list[dict] = []
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError("claude session timed out")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as e:
                raise TimeoutError("claude session timed out") from e
            if line is None:
                tail = "".join(self.stderr)[-500:]
                raise LLMError(f"claude exited before finishing the turn: {tail}")
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(e)
            if e.get("type") == "result":
                return events

    def close(self) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            self.proc.kill()
