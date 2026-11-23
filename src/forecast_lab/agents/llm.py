"""Thin LLM client layer. The agent loop depends only on the `LLMClient` protocol, so tests
use a scripted fake and real runs use `AnthropicLLM`.

Notes on current Claude models (verified for this project, recorded in docs/DECISIONS.md):
  * sampling parameters (temperature/top_p/top_k) are rejected on current models, so
    there is no temperature knob; variance is estimated with repeated runs instead;
  * forced tool_choice is rejected on some current models, so the agent is told to call
    `submit_forecast` and tool schemas are `strict`;
  * server-side refusal fallbacks are deliberately NOT enabled: a silent switch to another
    model would contaminate a model comparison. A refusal is logged as a failure.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

# USD per million tokens: (input, output, cache_read, cache_write_5m).
# Source: Anthropic pricing as of 2026-11 (see docs/DECISIONS.md). Update if prices change.
PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __iadd__(self, other: Usage) -> Usage:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        return self

    @property
    def total(self) -> int:
        return (self.input_tokens + self.output_tokens + self.cache_read_tokens
                + self.cache_write_tokens)

    def to_dict(self) -> dict:
        return asdict(self)


def cost_usd(model: str, u: Usage) -> float:
    if model not in PRICING:
        raise KeyError(f"No pricing for {model}; add it to PRICING before running.")
    i, o, cr, cw = PRICING[model]
    return (u.input_tokens * i + u.output_tokens * o + u.cache_read_tokens * cr
            + u.cache_write_tokens * cw) / 1e6


@dataclass
class LLMResponse:
    content: list[dict[str, Any]]  # content blocks as plain dicts, passed back verbatim
    stop_reason: str
    usage: Usage
    model: str
    latency_s: float
    request_id: str | None = None


class LLMError(RuntimeError):
    """Non-retryable API failure for one prediction (logged as a failure)."""


class LLMClient(Protocol):
    model: str

    def create(self, *, system: str, messages: list[dict], tools: list[dict],
               max_tokens: int) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(self, model: str, effort: str = "low", max_retries: int = 8,
                 timeout_s: float = 120.0) -> None:
        import anthropic

        self.model = model
        self.effort = effort
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(max_retries=max_retries, timeout=timeout_s)

    def create(self, *, system, messages, tools, max_tokens) -> LLMResponse:
        a = self._anthropic
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=max_tokens, system=system, messages=messages,
            thinking={"type": "adaptive"}, output_config={"effort": self.effort},
        )
        if tools:
            kwargs["tools"] = tools
        t0 = time.perf_counter()
        try:
            resp = self._client.messages.create(**kwargs)
        except a.RateLimitError as e:  # SDK already retried with backoff
            raise LLMError(f"rate limited after retries: {e}") from e
        except a.APIStatusError as e:
            raise LLMError(f"API status {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError(f"connection error: {e}") from e
        u = resp.usage
        return LLMResponse(
            content=[b.model_dump(mode="json", exclude_none=True) for b in resp.content],
            stop_reason=resp.stop_reason or "unknown",
            usage=Usage(u.input_tokens or 0, u.output_tokens or 0,
                        u.cache_read_input_tokens or 0, u.cache_creation_input_tokens or 0),
            model=resp.model,
            latency_s=time.perf_counter() - t0,
            request_id=getattr(resp, "_request_id", None),
        )
