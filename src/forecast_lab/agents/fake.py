"""Offline stand-ins for the LLM, for tests and pipeline dry runs ONLY.

Their outputs are not results and must never appear in a report as such: runs made with
them are forced to `exploratory` and written under `runs_fake/`.
"""

from __future__ import annotations

import json
import math
import re
from itertools import count

from forecast_lab.agents.llm import LLMResponse, Usage

_ids = count(1)


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def tool_use(name: str, args: dict) -> dict:
    return {"type": "tool_use", "id": f"toolu_fake_{next(_ids)}", "name": name, "input": args}


def response(blocks: list[dict], stop: str | None = None, model: str = "fake") -> LLMResponse:
    stop = stop or ("tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn")
    return LLMResponse(blocks, stop, Usage(100, 20), model, 0.0)


class ScriptedLLM:
    """Returns a fixed sequence of responses; records every request it receives."""

    def __init__(self, script: list[LLMResponse], model: str = "claude-haiku-5-5") -> None:
        self.model = model
        self.script = list(script)
        self.requests: list[dict] = []

    def create(self, *, system, messages, tools, max_tokens) -> LLMResponse:
        self.requests.append({"system": system, "messages": [dict(m) for m in messages],
                              "tools": tools})
        if not self.script:
            return response([text_block("I am done.")])
        return self.script.pop(0)


class HeuristicFakeLLM:
    """Looks up both teams' form, then submits a logistic of the margin difference.
    Writes a fixed note when asked to journal."""

    model = "claude-haiku-5-5"  # priced like Haiku so budget logic is exercised

    def create(self, *, system, messages, tools, max_tokens) -> LLMResponse:
        if not tools:
            return response([text_block("Recent margin was a decent but noisy signal.")])
        task = messages[0]["content"]
        m = re.search(r"tools: (\S+), (\S+)\.", task)
        home, away = m.group(1), m.group(2)
        last = messages[-1]
        if last["role"] == "user" and isinstance(last["content"], str):
            return response([tool_use("get_team_form", {"team": home, "last_n": 10}),
                             tool_use("get_team_form", {"team": away, "last_n": 10})])
        results = []
        for b in last["content"]:
            if b.get("type") == "tool_result":
                try:
                    results.append(json.loads(b["content"]))
                except json.JSONDecodeError:  # critique prompt is plain text
                    results.append({})
        margins = [r.get("summary", {}).get("avg_margin") for r in results]
        if len(margins) == 2 and None not in margins:
            p = 1 / (1 + math.exp(-(0.1 * (margins[0] - margins[1]) + 0.2)))
            p = min(max(p, 0.05), 0.95)
        else:  # e.g. the critique turn: resubmit
            p = 0.55
        return response([tool_use("submit_forecast",
                                  {"prob_home_win": round(p, 3),
                                   "reasoning": "Margin difference plus home edge."})])
