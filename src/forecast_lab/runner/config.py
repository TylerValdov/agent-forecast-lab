"""Experiment configuration (pre-registered YAML)."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# replay_post_cutoff: real names, but only games played after the model's training
# cutoff (`post_cutoff_after`), so outcomes cannot have been memorized.
Mode = Literal["live", "replay_anonymized", "replay_raw", "replay_post_cutoff"]
VariantId = Literal["A", "B", "C", "D", "R", "G"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GameSelection(_Strict):
    start: date
    end: date  # exclusive
    sample: int | None = None  # seeded subsample size (kept in chronological order)
    sample_seed: int = 0
    # optional nested subsample of the sample above (pairs two experiments game-for-game)
    subsample: int | None = None
    subsample_seed: int = 0


class AgentSettings(_Strict):
    max_steps: int = 8
    critique_extra_steps: int = 4
    max_tokens_per_call: int = 4000
    max_total_tokens: int = 60_000
    prompt_version: str = "v1"


class MemorySettings(_Strict):
    k: int = 10


class HintSettings(_Strict):
    rate: float = Field(0.2, ge=0, le=1)
    seed: int = 0


class RecallProbe(_Strict):
    """Tool-free 'what was the final score?' questions (contamination audit, RQ5).
    `n_pre` games are sampled from the experiment's games (possibly in training data);
    `n_post` from games after `post_after` (after the model's training cutoff), as the
    no-memory control."""

    n_pre: int = 150
    n_post: int = 100
    post_after: date
    seed: int = 0


class Budget(_Strict):
    usd: float
    tokens: int


class Comparison(_Strict):
    a: str  # variant id or baseline name
    b: str
    rq: str  # which research question this comparison addresses


class ExternalRef(_Strict):
    """A variant from another experiment's latest completed run, joined by game_id for
    paired comparisons (e.g. Sonnet's A vs Haiku's A on the same games)."""

    experiment: str
    variant: str
    label: str


class Evaluation(_Strict):
    reference_baseline: str = "elo"
    min_n: int = 100
    bootstrap_resamples: int = 10_000
    bootstrap_seed: int = 0
    comparisons: list[Comparison] = []
    external: list[ExternalRef] = []


class ExperimentConfig(_Strict):
    name: str
    description: str = ""
    status: Literal["preregistered", "exploratory"] = "preregistered"
    domain: Literal["nba"] = "nba"
    mode: Mode
    model: str
    effort: Literal["low", "medium", "high"] = "low"
    # "api": Anthropic API key (clean; billed per token). "claude_code": Claude Code CLI on
    # the user's subscription (adds Claude Code's own context; see DECISIONS D28).
    backend: Literal["api", "claude_code"] = "api"
    seed: int = 0
    repeats: int = 1
    variants: list[VariantId]
    lead_minutes: int = 60
    games: GameSelection
    agent: AgentSettings = AgentSettings()
    memory: MemorySettings = MemorySettings()
    hints: HintSettings = HintSettings()
    recall_probe: RecallProbe | None = None
    budget: Budget
    evaluation: Evaluation = Evaluation()
    # predictions for one game's variants run concurrently (no effect on results)
    concurrency: int = 4
    # seasons to load (default: configs/data.yaml); post-cutoff runs add the current season
    data_seasons: list[str] | None = None
    post_cutoff_after: date | None = None

    @model_validator(mode="after")
    def _post_cutoff_needs_date(self):
        if self.mode == "replay_post_cutoff" and self.post_cutoff_after is None:
            raise ValueError("mode replay_post_cutoff requires post_cutoff_after")
        return self


def load_config(path: str | Path) -> tuple[ExperimentConfig, str]:
    """-> (config, sha256 of the exact file bytes)."""
    raw = Path(path).read_bytes()
    return ExperimentConfig(**yaml.safe_load(raw)), hashlib.sha256(raw).hexdigest()
