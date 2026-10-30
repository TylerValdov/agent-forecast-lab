"""DataStore: the single, leakage-guarded read path for all historical data.

Contract (enforced here and nowhere else):
  * Every query that returns past information takes `as_of` and returns only records
    whose availability timestamp is STRICTLY before `as_of`.
  * `as_of` must be a timezone-aware datetime. Naive datetimes, dates, strings and None
    raise `AsOfError`.
  * Returned records are immutable (frozen pydantic models) and returned lists are fresh
    copies, so callers cannot mutate the store.
  * Outcomes of a game are only obtainable through `reveal()`, which requires a clock at or
    after the result's availability time.

Tools, baselines, memory and the runner must read data only through this class.
`tests/test_leakage.py` tries to break each of these guarantees.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Generic, TypeVar

from forecast_lab.data.schema import GameResult, InjurySnapshot, ScheduledGame, TeamBox


class AsOfError(ValueError):
    """Raised when a query is made without a valid, timezone-aware as_of."""


class RevealError(RuntimeError):
    """Raised when an outcome is requested before it is knowable."""


def check_as_of(as_of: object) -> datetime:
    if not isinstance(as_of, datetime):
        raise AsOfError(f"as_of must be a datetime, got {type(as_of).__name__}")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise AsOfError("as_of must be timezone-aware")
    return as_of


T = TypeVar("T")


class _TimeIndex(Generic[T]):
    """Records sorted by an availability key, queryable by strict prefix."""

    def __init__(self, records: Iterable[T], key) -> None:
        self._records: list[T] = sorted(records, key=key)
        self._keys: list[datetime] = [key(r) for r in self._records]

    def before(self, as_of: datetime) -> list[T]:
        # bisect_left -> index of first key >= as_of, so everything left of it is < as_of.
        return self._records[: bisect.bisect_left(self._keys, as_of)]


class DataStore:
    def __init__(
        self,
        results: Iterable[GameResult],
        schedule: Iterable[ScheduledGame],
        box: Iterable[TeamBox] = (),
        injuries: Iterable[InjurySnapshot] = (),
        source: str = "unknown",
    ) -> None:
        results = list(results)
        box = list(box)
        injuries = list(injuries)
        self.source = source
        self.injuries_available = bool(injuries)

        self._schedule: dict[str, ScheduledGame] = {g.game_id: g for g in schedule}
        for r in results:  # every result is also a scheduled game
            self._schedule.setdefault(r.game_id, r.to_scheduled())
        self._results_by_id = {r.game_id: r for r in results}

        def by_avail(r):
            return r.available_at

        self._results = _TimeIndex(results, by_avail)
        team_results: dict[str, list[GameResult]] = defaultdict(list)
        for r in results:
            team_results[r.home].append(r)
            team_results[r.away].append(r)
        self._team_results = {t: _TimeIndex(rs, by_avail) for t, rs in team_results.items()}

        team_box: dict[str, list[TeamBox]] = defaultdict(list)
        for b in box:
            team_box[b.team].append(b)
        self._team_box = {t: _TimeIndex(bs, by_avail) for t, bs in team_box.items()}

        team_inj: dict[str, list[InjurySnapshot]] = defaultdict(list)
        for s in injuries:
            team_inj[s.team].append(s)
        self._team_injuries = {
            t: _TimeIndex(ss, lambda s: s.published_at) for t, ss in team_inj.items()
        }
        self.snapshot_hash = self._compute_hash(results, box, injuries)

    # ----------------------------------------------------------------- public, no as_of
    # Schedule information (who plays whom, when) is published before the season and
    # contains no outcomes, so it does not need an as_of.

    def scheduled(self, game_id: str) -> ScheduledGame:
        return self._schedule[game_id]

    def scheduled_between(self, start: datetime, end: datetime) -> list[ScheduledGame]:
        check_as_of(start)
        check_as_of(end)
        games = [g for g in self._schedule.values() if start <= g.tipoff_utc < end]
        return sorted(games, key=lambda g: (g.tipoff_utc, g.game_id))

    def teams(self) -> list[str]:
        return sorted({g.home for g in self._schedule.values()})

    # ------------------------------------------------------------- as_of-guarded reads

    def results(
        self,
        as_of: datetime,
        team: str | None = None,
        last_n: int | None = None,
        opponent: str | None = None,
    ) -> list[GameResult]:
        """Finished games whose result was available strictly before `as_of`,
        oldest first. Optionally filtered to one team and/or one opponent."""
        as_of = check_as_of(as_of)
        if team is None:
            rows = self._results.before(as_of)
        else:
            idx = self._team_results.get(team)
            rows = idx.before(as_of) if idx else []
        if opponent is not None:
            rows = [r for r in rows if opponent in (r.home, r.away)]
        if last_n is not None:
            rows = rows[-last_n:] if last_n > 0 else []
        return list(rows)

    def team_box(self, as_of: datetime, team: str, last_n: int | None = None) -> list[TeamBox]:
        as_of = check_as_of(as_of)
        idx = self._team_box.get(team)
        rows = idx.before(as_of) if idx else []
        if last_n is not None:
            rows = rows[-last_n:] if last_n > 0 else []
        return list(rows)

    def latest_injury_report(self, as_of: datetime, team: str) -> InjurySnapshot | None:
        as_of = check_as_of(as_of)
        idx = self._team_injuries.get(team)
        rows = idx.before(as_of) if idx else []
        return rows[-1] if rows else None

    # ------------------------------------------------------------------------ reveal

    def reveal(self, game_id: str, clock: datetime) -> GameResult | None:
        """The outcome of a game, for scoring. `clock` is the simulated (replay) or real
        (live) current time; asking before the result is knowable raises RevealError.
        Returns None if the game has no result (e.g. postponed / not yet played)."""
        clock = check_as_of(clock)
        r = self._results_by_id.get(game_id)
        if r is None:
            return None
        if clock < r.available_at:
            raise RevealError(f"{game_id} result not available until {r.available_at}")
        return r

    # ---------------------------------------------------------------------- snapshot

    @staticmethod
    def _compute_hash(results, box, injuries) -> str:
        h = hashlib.sha256()
        for group in (results, box, injuries):
            for line in sorted(json.dumps(r.model_dump(mode="json"), sort_keys=True)
                               for r in group):
                h.update(line.encode())
            h.update(b"|")
        return h.hexdigest()[:16]
