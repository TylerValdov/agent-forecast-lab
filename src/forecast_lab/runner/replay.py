"""Chronological replay of a pre-registered experiment.

Per game, strictly in this order:
  1. predict with every variant (and repeat), appending each record to the ledger;
  2. reveal the outcome (clock = the result's availability time) into a SEPARATE file;
  3. only then write memory notes (variant B), timestamped at the reveal clock.
Games are never shuffled; a seeded subsample keeps chronological order.
"""

from __future__ import annotations

import json
import platform
import random
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from forecast_lab import __version__
from forecast_lab.agents import prompts
from forecast_lab.agents.agent import AgentConfig, write_note
from forecast_lab.agents.hints import make_hint, select_hint_games, verify_false
from forecast_lab.agents.llm import LLMClient
from forecast_lab.agents.memory import MemoryStore, Note
from forecast_lab.data.masker import Masker
from forecast_lab.data.schema import ScheduledGame
from forecast_lab.data.store import DataStore
from forecast_lab.ledger.jsonl import Ledger
from forecast_lab.runner.budget import BudgetGuard
from forecast_lab.runner.config import ExperimentConfig
from forecast_lab.runner.predict import RunContext, predict
from forecast_lab.runner.prereg import PreregRecord


def select_games(store: DataStore, cfg: ExperimentConfig, clock_end: datetime
                 ) -> list[ScheduledGame]:
    start = datetime.combine(cfg.games.start, time(), tzinfo=UTC)
    end = datetime.combine(cfg.games.end, time(), tzinfo=UTC)
    games = [g for g in store.scheduled_between(start, end)
             if store.reveal(g.game_id, clock_end) is not None]  # finished games only
    if cfg.games.sample is not None and cfg.games.sample < len(games):
        keep = set(random.Random(cfg.games.sample_seed).sample(
            [g.game_id for g in games], cfg.games.sample))
        games = [g for g in games if g.game_id in keep]
    return games  # chronological (scheduled_between sorts by tipoff)


def note_summary(rec: dict, home_win: bool, masker: Masker) -> str:
    home, away = masker.team(rec["home"]), masker.team(rec["away"])
    winner = home if home_win else away
    return (f"Game: {away} at {home}. You forecast P(home win) = {rec['prob']:.2f}. "
            f"Your reasoning: {rec['reasoning']}\nOutcome: {winner} won.")


def run_replay(cfg: ExperimentConfig, prereg: PreregRecord, store: DataStore,
               llm: LLMClient, out_root: Path = Path("runs")) -> Path:
    if cfg.mode == "live":
        raise ValueError("use the live runner for mode=live")
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    out = out_root / cfg.name / run_id
    out.mkdir(parents=True)
    ledger, outcomes = Ledger(out / "predictions.jsonl"), Ledger(out / "outcomes.jsonl")
    masker = Masker(salt=f"{cfg.name}:{cfg.seed}", enabled=cfg.mode == "replay_anonymized")
    agent_cfg = AgentConfig(**cfg.agent.model_dump())
    ctx = RunContext(run_id, prereg.config_sha256, cfg.mode, masker, agent_cfg,
                     cfg.lead_minutes, cfg.memory.k, prereg.exploratory)
    memories = {r: MemoryStore(out / f"memory_r{r}.jsonl") for r in range(cfg.repeats)}
    budget = BudgetGuard(cfg.budget.usd, cfg.budget.tokens)
    lead = timedelta(minutes=cfg.lead_minutes)
    far_future = datetime(2100, 1, 1, tzinfo=UTC)

    games = select_games(store, cfg, far_future)
    hint_ids = select_hint_games([g.game_id for g in games], cfg.hints.rate, cfg.hints.seed) \
        if "D" in cfg.variants else set()

    manifest = {
        "run_id": run_id, "experiment": cfg.name, "status": cfg.status,
        "prereg": asdict(prereg), "mode": cfg.mode, "model": cfg.model, "effort": cfg.effort,
        "seed": cfg.seed, "repeats": cfg.repeats, "variants": cfg.variants,
        "prompt_version": agent_cfg.prompt_version,
        "prompt_bundle_hash": prompts.bundle_hash(agent_cfg.prompt_version),
        "data_snapshot": store.snapshot_hash, "data_source": store.source,
        "n_games_selected": len(games), "hint_game_ids": sorted(hint_ids),
        "budget": cfg.budget.model_dump(), "started_at": datetime.now(UTC).isoformat(),
        "forecast_lab_version": __version__, "python": platform.python_version(),
        "config": json.loads(cfg.model_dump_json()),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    n_done = 0
    for g in games:
        as_of = g.tipoff_utc - lead
        hint = None
        if g.game_id in hint_ids:
            hint = make_hint(store, g, as_of, masker, cfg.hints.seed)
            assert hint is None or verify_false(store, g, as_of, hint)
        game_records: dict[int, dict] = {}
        logged = 0
        for r in range(cfg.repeats):
            for v in cfg.variants:
                if v == "D" and g.game_id not in hint_ids:
                    continue  # D is defined only on the hint subset; pair is A on same game
                if v == "D" and hint is None:
                    ledger.append({"kind": "skip", "run_id": run_id, "game_id": g.game_id,
                                   "variant": "D", "repeat": r,
                                   "reason": "no verifiably-false hint template applied"})
                    continue
                if not budget.can_afford_next():
                    break
                rec = predict(llm, store, g, as_of, v, ctx, memory=memories[r], hint=hint,
                              repeat=r)
                ledger.append(rec)
                budget.charge(rec["cost_usd"], sum(rec["tokens"].values()))
                n_done += 1
                logged += 1
                if v == "B":
                    game_records[r] = rec
        if logged == 0 and budget.stopped_reason:
            break

        # ---- reveal (after all predictions for this game are on disk)
        result = store.reveal(g.game_id, clock=store.reveal(g.game_id, far_future).available_at)
        outcomes.append({"game_id": g.game_id, "home_win": result.home_win,
                         "home_pts": result.home_pts, "away_pts": result.away_pts,
                         "revealed_at": result.available_at.isoformat(), "run_id": run_id,
                         "written_at": datetime.now(UTC).isoformat()})

        # ---- memory update (only after reveal)
        for r, rec in game_records.items():
            if rec["prob"] is None:
                continue
            text, usage, cost = write_note(llm, note_summary(rec, result.home_win, masker),
                                           agent_cfg.prompt_version)
            budget.charge(cost, usage.total)
            if text:
                memories[r].add(Note(f"{run_id}:{g.game_id}:r{r}", result.available_at,
                                     g.game_id, masker.text(text)))
        if budget.stopped_reason:
            break

    manifest |= {"finished_at": datetime.now(UTC).isoformat(), "n_predictions": n_done,
                 "usd_spent": round(budget.usd_spent, 6), "tokens_spent": budget.tokens_spent,
                 "stopped_reason": budget.stopped_reason or "completed"}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return out
