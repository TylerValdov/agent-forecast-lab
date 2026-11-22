"""The forecasting agent loop (manual tool-use loop, so step limits, token caps, trajectory
logging and the draft/critique phase are all under our control).

The conversation is append-only: assistant content blocks are passed back verbatim
(including thinking blocks), and nothing earlier in `messages` is ever edited.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from forecast_lab.agents import prompts
from forecast_lab.agents.llm import LLMClient, LLMError, Usage, cost_usd
from forecast_lab.tools.nba_tools import NBAToolbox, parse_forecast

NO_SUBMIT_NUDGE = "Please answer by calling submit_forecast."

# Failure codes (recorded, never silently replaced by a default).
FAIL_STEP_LIMIT = "step_limit"
FAIL_TOKEN_LIMIT = "token_limit"
FAIL_REFUSAL = "refusal"
FAIL_API = "api_error"
FAIL_MAX_TOKENS = "max_tokens_truncated"


@dataclass
class AgentConfig:
    max_steps: int = 8
    critique_extra_steps: int = 4
    max_tokens_per_call: int = 4000
    max_total_tokens: int = 60_000  # per prediction, across all calls
    prompt_version: str = "v1"


@dataclass
class AgentResult:
    prob: float | None
    reasoning: str | None
    draft_prob: float | None
    draft_reasoning: str | None
    failure: str | None
    steps: int
    usage: Usage
    cost_usd: float
    latency_s: float
    trajectory: list[dict] = field(default_factory=list)
    invalid_submissions: int = 0
    tool_errors: int = 0


def run_agent(llm: LLMClient, toolbox: NBAToolbox, task: str, cfg: AgentConfig,
              critique: bool = False, system_extra: str | None = None) -> AgentResult:
    system = prompts.load("system", cfg.prompt_version)
    if system_extra:
        system = f"{system}\n\n{system_extra}"
    tools = toolbox.specs()
    messages: list[dict] = [{"role": "user", "content": task}]
    trajectory: list[dict] = [{"type": "task", "text": task}]
    usage = Usage()
    latency = 0.0
    draft: tuple[float, str] | None = None
    invalid = 0
    max_steps = cfg.max_steps + (cfg.critique_extra_steps if critique else 0)

    def result(prob=None, reasoning=None, failure=None, steps=0) -> AgentResult:
        return AgentResult(
            prob=prob, reasoning=reasoning,
            draft_prob=draft[0] if draft else None, draft_reasoning=draft[1] if draft else None,
            failure=failure, steps=steps, usage=usage, cost_usd=cost_usd(llm.model, usage),
            latency_s=latency, trajectory=trajectory, invalid_submissions=invalid,
            tool_errors=sum(1 for c in toolbox.calls if c["is_error"]),
        )

    for step in range(1, max_steps + 1):
        if usage.total >= cfg.max_total_tokens:
            return result(failure=FAIL_TOKEN_LIMIT, steps=step - 1)
        try:
            resp = llm.create(system=system, messages=messages, tools=tools,
                              max_tokens=cfg.max_tokens_per_call)
        except LLMError as e:
            trajectory.append({"type": "api_error", "error": str(e)})
            return result(failure=FAIL_API, steps=step)
        usage += resp.usage
        latency += resp.latency_s
        trajectory.append({"type": "assistant", "step": step, "stop_reason": resp.stop_reason,
                           "content": resp.content, "usage": resp.usage.to_dict()})
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "refusal":
            return result(failure=FAIL_REFUSAL, steps=step)

        tool_uses = [b for b in resp.content if b.get("type") == "tool_use"]
        if not tool_uses:
            if resp.stop_reason == "max_tokens":
                return result(failure=FAIL_MAX_TOKENS, steps=step)
            messages.append({"role": "user", "content": NO_SUBMIT_NUDGE})
            trajectory.append({"type": "nudge", "text": NO_SUBMIT_NUDGE})
            continue

        results: list[dict] = []
        final = None
        for tu in tool_uses:
            name, args = tu["name"], tu.get("input", {})
            if name == "submit_forecast":
                fc, err = parse_forecast(args)
                if fc is None:
                    invalid += 1
                    out, is_err = json.dumps({"error": err}), True
                elif critique and draft is None:
                    draft = (fc.prob_home_win, fc.reasoning)
                    out = prompts.load("critique", cfg.prompt_version).format(
                        draft_prob=fc.prob_home_win)
                    is_err = False
                else:
                    final = fc
                    out, is_err = json.dumps({"status": "recorded"}), False
            else:
                out, is_err = toolbox.execute(name, args)
            results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out,
                            **({"is_error": True} if is_err else {})})
            trajectory.append({"type": "tool_result", "name": name, "input": args,
                               "output": out, "is_error": is_err})
        if final is not None:
            return result(prob=final.prob_home_win, reasoning=final.reasoning, steps=step)
        messages.append({"role": "user", "content": results})

    return result(failure=FAIL_STEP_LIMIT, steps=max_steps)


def write_note(llm: LLMClient, summary: str, prompt_version: str = "v1",
               max_tokens: int = 1000) -> tuple[str | None, Usage, float]:
    """Post-outcome memory note (variant B). Returns (text or None on failure, usage, cost)."""
    system = prompts.load("note_writer", prompt_version)
    try:
        resp = llm.create(system=system, messages=[{"role": "user", "content": summary}],
                          tools=[], max_tokens=max_tokens)
    except LLMError:
        return None, Usage(), 0.0
    text = " ".join(b.get("text", "") for b in resp.content if b.get("type") == "text").strip()
    words = text.split()
    if len(words) > 80:  # enforce the cap; the note is memory, keep it small
        text = " ".join(words[:80])
    return (text or None), resp.usage, cost_usd(llm.model, resp.usage)
