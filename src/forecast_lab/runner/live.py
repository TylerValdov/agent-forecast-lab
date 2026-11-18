"""Live mode: forecasts for upcoming games, made and committed BEFORE tip-off.

Two jobs, run on a schedule:
  * `predict`: refresh data, find games tipping off within the horizon, run every
    registered variant + baselines with as_of = now, append to the live ledger.
  * `resolve`: refresh data, write outcomes for finished games to a separate ledger, then
    write memory notes (variant B).

`--dry-run --now <ISO time>` replays the same code path at a simulated clock against cached
data, writing to a separate directory. That is how the job is tested without waiting a day.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from forecast_lab.agents.agent import AgentConfig, write_note
from forecast_lab.agents.claude_code import UsageLimitReached
from forecast_lab.agents.hints import make_hint, verify_false
from forecast_lab.agents.llm import LLMClient
from forecast_lab.agents.memory import MemoryStore, Note
from forecast_lab.baselines.market import MarketSource, NoMarket
from forecast_lab.baselines.ml import WalkForwardML
from forecast_lab.data.masker import Masker
from forecast_lab.data.store import DataStore, RevealError
from forecast_lab.ledger.jsonl import Ledger
from forecast_lab.runner.baselines import default_forecasters
from forecast_lab.runner.budget import BudgetGuard
from forecast_lab.runner.config import ExperimentConfig
from forecast_lab.runner.predict import RunContext, predict
from forecast_lab.runner.prereg import PreregRecord
from forecast_lab.runner.replay import note_summary

MIN_LEAD = timedelta(minutes=10)  # never forecast a game this close to (or after) tip-off


def is_hint_game(game_id: str, rate: float, seed: int) -> bool:
    return random.Random(f"hint:{seed}:{game_id}").random() < rate


def _already(ledger: Ledger) -> set[tuple[str, str, str]]:
    return {(r.get("kind", ""), r.get("game_id", ""), r.get("variant") or r.get("model", ""))
            for r in ledger.records()}


def live_predict(cfg: ExperimentConfig, prereg: PreregRecord, store: DataStore,
                 llm: LLMClient | None, now: datetime, out_dir: Path,
                 horizon: timedelta = timedelta(hours=30),
                 market: MarketSource | None = None) -> dict:
    ledger = Ledger(out_dir / "predictions.jsonl")
    done = _already(ledger)
    upcoming = store.scheduled_between(now + MIN_LEAD, now + horizon)
    run_id = f"live-{now:%Y%m%dT%H%MZ}"
    summary = {"run_id": run_id, "now": now.isoformat(), "games": len(upcoming),
               "agent_predictions": 0, "baseline_predictions": 0}
    if not upcoming:
        return summary

    # ---- baselines: train online on everything known at `now`, then forecast
    fs = default_forecasters(timedelta(minutes=cfg.lead_minutes))
    for f in fs:
        if isinstance(f, WalkForwardML):
            f.bind(store)
    for r in store.results(now):
        for f in fs:
            f.observe(r)
    market = market or NoMarket()
    quotes = market.quotes(upcoming, now)
    for g in upcoming:
        for f in fs:
            if ("baseline", g.game_id, f.name) in done:
                continue
            ledger.append({"kind": "baseline", "run_id": run_id, "mode": "live",
                           "game_id": g.game_id, "home": g.home, "away": g.away,
                           "tipoff_utc": g.tipoff_utc.isoformat(), "model": f.name,
                           "as_of": now.isoformat(), "prob": f.predict(store, g, now),
                           "timestamp": datetime.now(UTC).isoformat()})
            summary["baseline_predictions"] += 1
        q = quotes.get(g.game_id)
        if q and ("baseline", g.game_id, "market") not in done:
            ledger.append({"kind": "baseline", "run_id": run_id, "mode": "live",
                           "game_id": g.game_id, "model": "market", "as_of": now.isoformat(),
                           "prob": q.prob_home, "n_books": q.n_books, "source": q.source,
                           "tipoff_utc": g.tipoff_utc.isoformat(),
                           "timestamp": datetime.now(UTC).isoformat()})

    if llm is None:
        return summary

    # ---- agents
    masker = Masker("live", enabled=False)  # live games can't be memorized: no masking
    ctx = RunContext(run_id, prereg.config_sha256, "live", masker,
                     AgentConfig(**cfg.agent.model_dump()), cfg.lead_minutes, cfg.memory.k,
                     prereg.exploratory)
    budget = BudgetGuard(cfg.budget.usd, cfg.budget.tokens)
    memory = MemoryStore(out_dir / "memory_r0.jsonl")
    for g in upcoming:
        hint = None
        if "D" in cfg.variants and is_hint_game(g.game_id, cfg.hints.rate, cfg.hints.seed):
            hint = make_hint(store, g, now, masker, cfg.hints.seed)
            assert hint is None or verify_false(store, g, now, hint)
        for v in cfg.variants:
            if ("agent", g.game_id, v) in done or (v == "D" and hint is None):
                continue
            if not budget.can_afford_next():
                summary["stopped_reason"] = budget.stopped_reason
                return summary
            # Lead time shown to the agent is the real time remaining.
            ctx.lead_minutes = int((g.tipoff_utc - now).total_seconds() // 60)
            try:
                rec = predict(llm, store, g, now, v, ctx, memory=memory, hint=hint)
            except UsageLimitReached as e:
                summary["stopped_reason"] = f"subscription usage limit: {e}"
                return summary
            ledger.append(rec)
            budget.charge(rec["cost_usd"], sum(rec["tokens"].values()))
            summary["agent_predictions"] += 1
    summary["usd_spent"] = round(budget.usd_spent, 6)
    return summary


def live_resolve(store: DataStore, llm: LLMClient | None, now: datetime, out_dir: Path,
                 prompt_version: str = "v1") -> dict:
    preds = Ledger(out_dir / "predictions.jsonl")
    outcomes = Ledger(out_dir / "outcomes.jsonl")
    memory = MemoryStore(out_dir / "memory_r0.jsonl")
    resolved = {r["game_id"] for r in outcomes.records()}
    records = [r for r in preds.records() if r.get("kind") in ("agent", "baseline")]
    pending = sorted({r["game_id"] for r in records} - resolved)
    n = 0
    for gid in pending:
        try:
            res = store.reveal(gid, clock=now)
        except RevealError:
            continue  # not final yet
        if res is None:
            continue
        outcomes.append({"game_id": gid, "home_win": res.home_win, "home_pts": res.home_pts,
                         "away_pts": res.away_pts, "revealed_at": now.isoformat()})
        n += 1
        if llm is None:
            continue
        for rec in records:
            if rec["game_id"] == gid and rec.get("variant") == "B" and rec.get("prob"):
                try:
                    text, _, _ = write_note(llm, note_summary(rec, res.home_win,
                                                              Masker("", False)),
                                            prompt_version)
                except UsageLimitReached:
                    text = None  # note skipped; memory stays time-gated, so this is safe
                if text:
                    memory.add(Note(f"live:{gid}", now, gid, text))
    return {"now": now.isoformat(), "resolved": n, "still_pending": len(pending) - n}
