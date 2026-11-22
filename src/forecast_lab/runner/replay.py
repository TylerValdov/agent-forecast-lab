"""Chronological replay of a pre-registered experiment.

Per game, strictly in this order:
  1. predict with every variant (and repeat), appending each record to the ledger;
  2. reveal the outcome (clock = the result's availability time) into a SEPARATE file;
  3. only then write memory notes (variant B), timestamped at the reveal clock.
Games are never shuffled; a seeded subsample keeps chronological order.

Runs are resumable (`resume_dir`): every prediction, outcome and memory note is on disk
before the next step starts, so a run stopped by a budget cap, a subscription usage limit
or a crash continues where it stopped, under the same run_id and config hash.
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
from forecast_lab.agents.claude_code import UsageLimitReached
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
from forecast_lab.runner.probe import ask, probe_items


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


class ResumeError(RuntimeError):
    pass


def _spent(records: list[dict]) -> tuple[float, int]:
    usd = sum(r.get("cost_usd") or 0.0 for r in records
              if r.get("kind") in ("agent", "note", "probe"))
    tok = sum(sum((r.get("tokens") or {}).values()) for r in records
              if r.get("kind") in ("agent", "note", "probe"))
    return usd, tok


def run_replay(cfg: ExperimentConfig, prereg: PreregRecord, store: DataStore,
               llm: LLMClient, out_root: Path = Path("runs"),
               resume_dir: Path | None = None,
               post_store: DataStore | None = None,
               clock: datetime | None = None) -> Path:
    """`post_store` holds games after the model's training cutoff, used only by the
    recall probe's control group (cfg.recall_probe)."""
    if cfg.mode == "live":
        raise ValueError("use the live runner for mode=live")
    backend = getattr(llm, "backend", "api")
    if backend != cfg.backend and not prereg.exploratory:
        raise ValueError(f"config says backend={cfg.backend} but the client is {backend}")
    if resume_dir is not None:
        out = Path(resume_dir)
        old = json.loads((out / "manifest.json").read_text())
        if old["prereg"]["config_sha256"] != prereg.config_sha256:
            raise ResumeError("config changed since this run started; start a new run")
        if old["data_snapshot"] != store.snapshot_hash:
            raise ResumeError("data snapshot changed since this run started")
        if old.get("backend", "api") != backend:
            raise ResumeError("cannot resume a run with a different backend")
        run_id = old["run_id"]
    else:
        run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
        out = out_root / cfg.name / run_id
        out.mkdir(parents=True)
    ledger, outcomes = Ledger(out / "predictions.jsonl"), Ledger(out / "outcomes.jsonl")
    masker = Masker(salt=f"{cfg.name}:{cfg.seed}", enabled=cfg.mode == "replay_anonymized")
    agent_cfg = AgentConfig(**cfg.agent.model_dump())
    ctx = RunContext(run_id, prereg.config_sha256, cfg.mode, masker, agent_cfg,
                     cfg.lead_minutes, cfg.memory.k, prereg.exploratory)
    memories = {r: MemoryStore(out / f"memory_r{r}.jsonl") for r in range(cfg.repeats)}
    lead = timedelta(minutes=cfg.lead_minutes)
    far_future = datetime(2100, 1, 1, tzinfo=UTC)

    prior = ledger.records()
    done = {(r["game_id"], r["variant"], r.get("repeat", 0)) for r in prior
            if r.get("kind") in ("agent", "skip")}
    noted = {r["note_id"] for r in prior if r.get("kind") == "note"}
    b_records = {(r["game_id"], r.get("repeat", 0)): r for r in prior
                 if r.get("kind") == "agent" and r["variant"] == "B"}
    revealed = {r["game_id"] for r in outcomes.records()}
    budget = BudgetGuard(cfg.budget.usd, cfg.budget.tokens)
    usd0, tok0 = _spent(prior)
    n0 = sum(1 for r in prior if r.get("kind") in ("agent", "note", "probe"))
    if n0:
        budget.usd_spent, budget.tokens_spent, budget.n_charges = usd0, tok0, n0

    games = select_games(store, cfg, far_future)
    hint_ids = select_hint_games([g.game_id for g in games], cfg.hints.rate, cfg.hints.seed) \
        if "D" in cfg.variants else set()

    manifest = {
        "run_id": run_id, "experiment": cfg.name, "status": cfg.status,
        "prereg": asdict(prereg), "mode": cfg.mode, "model": cfg.model, "effort": cfg.effort,
        "backend": backend, "seed": cfg.seed, "repeats": cfg.repeats, "variants": cfg.variants,
        "prompt_version": agent_cfg.prompt_version,
        "prompt_bundle_hash": prompts.bundle_hash(agent_cfg.prompt_version),
        "data_snapshot": store.snapshot_hash, "data_source": store.source,
        "n_games_selected": len(games), "hint_game_ids": sorted(hint_ids),
        "budget": cfg.budget.model_dump(), "forecast_lab_version": __version__,
        "python": platform.python_version(), "config": json.loads(cfg.model_dump_json()),
        "extra_prompt_hashes": {
            **({"guardrail": prompts.prompt_hash("guardrail", agent_cfg.prompt_version)}
               if "G" in cfg.variants else {}),
            **({"recall_probe": prompts.prompt_hash("recall_probe", agent_cfg.prompt_version)}
               if cfg.recall_probe else {}),
        },
        "post_cutoff_snapshot": post_store.snapshot_hash if post_store else None,
    }
    if resume_dir is not None:
        manifest["started_at"] = old["started_at"]
        manifest["resumes"] = old.get("resumes", []) + [datetime.now(UTC).isoformat()]
    else:
        manifest["started_at"] = datetime.now(UTC).isoformat()

    def save(stop: str | None) -> None:
        recs = ledger.records()
        usd, tok = _spent(recs)
        manifest.update({
            "updated_at": datetime.now(UTC).isoformat(),
            "n_predictions": sum(1 for r in recs if r.get("kind") == "agent"),
            "n_outcomes": len(outcomes.records()),
            "usd_spent": round(usd, 6), "tokens_spent": tok,
            "stopped_reason": stop,
        })
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    save("running")
    stop: str | None = None
    try:
        for g in games:
            as_of = g.tipoff_utc - lead
            hint = None
            if g.game_id in hint_ids:
                hint = make_hint(store, g, as_of, masker, cfg.hints.seed)
                assert hint is None or verify_false(store, g, as_of, hint)
            complete = True
            for r in range(cfg.repeats):
                for v in cfg.variants:
                    if v == "D" and g.game_id not in hint_ids:
                        continue  # D only on the hint subset; its pair is A on the same game
                    if (g.game_id, v, r) in done:
                        continue
                    if v == "D" and hint is None:
                        ledger.append({"kind": "skip", "run_id": run_id,
                                       "game_id": g.game_id, "variant": "D", "repeat": r,
                                       "reason": "no verifiably-false hint template applied"})
                        continue
                    if not budget.can_afford_next():
                        complete = False
                        break
                    rec = predict(llm, store, g, as_of, v, ctx, memory=memories[r],
                                  hint=hint, repeat=r)
                    ledger.append(rec)
                    budget.charge(rec["cost_usd"], sum(rec["tokens"].values()))
                    if v == "B":
                        b_records[(g.game_id, r)] = rec
            if not complete:
                stop = budget.stopped_reason
                break  # never reveal a game whose predictions are incomplete

            # ---- reveal (only after every prediction for this game is on disk)
            result = store.reveal(g.game_id,
                                  clock=store.reveal(g.game_id, far_future).available_at)
            if g.game_id not in revealed:
                outcomes.append({"game_id": g.game_id, "home_win": result.home_win,
                                 "home_pts": result.home_pts, "away_pts": result.away_pts,
                                 "revealed_at": result.available_at.isoformat(),
                                 "run_id": run_id,
                                 "written_at": datetime.now(UTC).isoformat()})
                revealed.add(g.game_id)

            # ---- memory update (only after reveal)
            for r in range(cfg.repeats):
                rec = b_records.get((g.game_id, r))
                note_id = f"{run_id}:{g.game_id}:r{r}"
                if rec is None or rec["prob"] is None or note_id in noted:
                    continue
                text, usage, cost = write_note(llm, note_summary(rec, result.home_win, masker),
                                               agent_cfg.prompt_version)
                budget.charge(cost, usage.total)
                ledger.append({"kind": "note", "run_id": run_id, "note_id": note_id,
                               "game_id": g.game_id, "repeat": r, "text": text,
                               "created_at": result.available_at.isoformat(),
                               "cost_usd": cost, "tokens": usage.to_dict()})
                noted.add(note_id)
                if text:
                    memories[r].add(Note(note_id, result.available_at, g.game_id,
                                         masker.text(text)))
            save("running")
            if budget.stopped_reason:
                stop = budget.stopped_reason
                break

        # ---- recall probe (contamination audit): tool-free "what was the score?"
        if cfg.recall_probe and not stop:
            rp = cfg.recall_probe
            finished = [store.reveal(g.game_id, far_future) for g in games]
            items = probe_items(finished, post_store, rp.n_pre, rp.n_post, rp.post_after,
                                rp.seed, clock or datetime.now(UTC))
            probed = {(r["game_id"], r["group"]) for r in prior if r.get("kind") == "probe"}
            for item in items:
                if (item.game.game_id, item.group) in probed:
                    continue
                if not budget.can_afford_next():
                    stop = budget.stopped_reason
                    break
                rec = ask(llm, item, agent_cfg.prompt_version) | {"run_id": run_id}
                ledger.append(rec)
                budget.charge(rec["cost_usd"], sum((rec.get("tokens") or {}).values()))
            save("running")
    except UsageLimitReached as e:
        stop = f"subscription usage limit reached (resume later with --resume): {e}"
    except BaseException as e:  # record the crash, never leave the manifest at "running"
        save(f"error: {type(e).__name__}: {e} (fix, then --resume)")
        raise
    save(stop or "completed")
    return out
