"""Walk-forward engine for non-LLM baselines.

For each game, in chronological order of `as_of`:
  1. every result that became available strictly before `as_of` is fed to `observe()`
     (results are pulled through the guarded DataStore, never from a raw table);
  2. every forecaster is asked for `predict()`.

So an online model (Elo) can only ever have seen the past, and a batch model (logistic,
GBM) can only train on what `observe()` gave it or on store queries at `as_of`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from forecast_lab.data.schema import GameResult, ScheduledGame
from forecast_lab.data.store import DataStore


class Forecaster(Protocol):
    name: str

    def predict(self, store: DataStore, game: ScheduledGame, as_of: datetime) -> float | None:
        """P(home win), or None if the model can't forecast yet (e.g. not enough history)."""

    def observe(self, result: GameResult) -> None:
        """Called once per finished game, only after its result is available."""


@dataclass(frozen=True)
class BaselinePrediction:
    game_id: str
    model: str
    as_of: datetime
    prob: float | None


class LeakageViolation(AssertionError):
    pass


def walk_forward(
    store: DataStore,
    games: list[ScheduledGame],
    forecasters: list[Forecaster],
    lead: timedelta = timedelta(hours=1),
) -> list[BaselinePrediction]:
    games = sorted(games, key=lambda g: (g.tipoff_utc, g.game_id))
    seen = 0
    out: list[BaselinePrediction] = []
    for g in games:
        as_of = g.tipoff_utc - lead
        known = store.results(as_of)  # oldest-available first; a growing prefix
        for r in known[seen:]:
            if r.available_at >= as_of:  # defensive: the store guarantees this
                raise LeakageViolation(f"{r.game_id} fed to models at {as_of}")
            for f in forecasters:
                f.observe(r)
        seen = len(known)
        for f in forecasters:
            out.append(BaselinePrediction(g.game_id, f.name, as_of, f.predict(store, g, as_of)))
    return out
