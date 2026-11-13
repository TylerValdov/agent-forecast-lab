"""One prediction for one (game, variant): build the task, run the agent, return a ledger
record. Shared by replay and live mode so both run identical agent code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from forecast_lab.agents.agent import AgentConfig, run_agent
from forecast_lab.agents.hints import Hint
from forecast_lab.agents.llm import LLMClient
from forecast_lab.agents.memory import MemoryStore, render_memory
from forecast_lab.data.masker import Masker
from forecast_lab.data.schema import ScheduledGame
from forecast_lab.data.store import DataStore
from forecast_lab.tools.nba_tools import NBAToolbox

VARIANT_NAMES = {"A": "baseline", "B": "memory", "C": "self_critique", "D": "hint_injected"}


@dataclass
class RunContext:
    run_id: str
    config_hash: str
    mode: str
    masker: Masker
    agent_cfg: AgentConfig
    lead_minutes: int
    memory_k: int = 10
    exploratory: bool = False


def build_task(game: ScheduledGame, masker: Masker, lead_minutes: int,
               memory_text: str | None = None, hint_text: str | None = None) -> str:
    home, away = masker.team(game.home), masker.team(game.away)
    when = ("date withheld" if masker.enabled
            else f"{game.game_date.isoformat()}, season {game.season}")
    parts = [
        f"Game: {away} (away) at {home} (home), {when}.",
        f"It tips off in about {lead_minutes} minutes. Use these exact team identifiers "
        f"with the tools: {home}, {away}.",
        f"What is the probability that {home} (the home team) wins?",
    ]
    if memory_text:
        parts.insert(0, memory_text + "\n")
    if hint_text:
        parts.insert(-1, masker.text(hint_text))
    return "\n".join(parts)


def predict(llm: LLMClient, store: DataStore, game: ScheduledGame, as_of: datetime,
            variant: str, ctx: RunContext, memory: MemoryStore | None = None,
            hint: Hint | None = None, repeat: int = 0) -> dict:
    loaded = memory.load(as_of, ctx.memory_k) if (variant == "B" and memory) else []
    task = build_task(
        game, ctx.masker, ctx.lead_minutes,
        memory_text=render_memory(loaded) if variant == "B" else None,
        hint_text=hint.text if (variant == "D" and hint) else None,
    )
    if ctx.masker.enabled:
        leaked = ctx.masker.leaked_terms(task)
        if leaked:  # refuse to send a prompt that names a real team in anonymized mode
            raise RuntimeError(f"anonymization leak in task for {game.game_id}: {leaked}")
    toolbox = NBAToolbox(store, game, as_of, ctx.masker,
                         injuries_enabled=store.injuries_available)
    res = run_agent(llm, toolbox, task, ctx.agent_cfg, critique=(variant == "C"))
    return {
        "kind": "agent",
        "run_id": ctx.run_id,
        "config_hash": ctx.config_hash,
        "exploratory": ctx.exploratory,
        "mode": ctx.mode,
        "game_id": game.game_id,
        "home": game.home,
        "away": game.away,
        "tipoff_utc": game.tipoff_utc.isoformat(),
        "variant": variant,
        "variant_name": VARIANT_NAMES[variant],
        "repeat": repeat,
        "model": llm.model,
        "prompt_version": ctx.agent_cfg.prompt_version,
        "as_of": as_of.isoformat(),
        "prob": res.prob,
        "reasoning": res.reasoning,
        "draft_prob": res.draft_prob,
        "draft_reasoning": res.draft_reasoning,
        "failure": res.failure,
        "steps": res.steps,
        "invalid_submissions": res.invalid_submissions,
        "tool_errors": res.tool_errors,
        "loaded_memory": [n.to_json() for n in loaded],
        "hint": None if hint is None or variant != "D" else {
            "template": hint.template, "target_team": hint.target_team,
            "direction": hint.direction, "text": hint.text, "ground_truth": hint.ground_truth,
        },
        "tokens": res.usage.to_dict(),
        "cost_usd": res.cost_usd,
        "latency_s": round(res.latency_s, 3),
        "trajectory": res.trajectory,
        "timestamp": datetime.now(UTC).isoformat(),
    }
