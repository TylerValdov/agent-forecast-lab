"""Derived statistics shared by agent tools and baseline features.

Everything here takes a DataStore and an `as_of`, and reads only through the store's
guarded API, so derived values inherit the leakage guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from forecast_lab.data.store import DataStore

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class FormSummary:
    games: int
    wins: int
    avg_margin: float
    avg_net_rating: float | None
    season_games: int
    season_wins: int

    @property
    def win_pct(self) -> float:
        return self.wins / self.games if self.games else 0.5

    @property
    def season_win_pct(self) -> float:
        return self.season_wins / self.season_games if self.season_games else 0.5


def form_summary(store: DataStore, as_of: datetime, team: str, last_n: int,
                 season: str | None = None) -> FormSummary:
    recent = store.results(as_of, team=team, last_n=last_n)
    wins = sum(1 for r in recent if (r.home == team) == r.home_win)
    margins = [(r.home_pts - r.away_pts) * (1 if r.home == team else -1) for r in recent]
    box = store.team_box(as_of, team, last_n=last_n)
    nr = [b.net_rating for b in box]
    season_games = season_wins = 0
    if season is not None:
        all_games = store.results(as_of, team=team)
        this = [r for r in all_games if r.season == season]
        season_games = len(this)
        season_wins = sum(1 for r in this if (r.home == team) == r.home_win)
    return FormSummary(
        games=len(recent),
        wins=wins,
        avg_margin=sum(margins) / len(margins) if margins else 0.0,
        avg_net_rating=sum(nr) / len(nr) if nr else None,
        season_games=season_games,
        season_wins=season_wins,
    )


@dataclass(frozen=True)
class RestInfo:
    days_rest: int | None  # full calendar days between last game and this one; None = no prior
    back_to_back: bool
    games_last_7_days: int


def rest_info(store: DataStore, as_of: datetime, team: str, game_tipoff: datetime) -> RestInfo:
    """Rest before a game tipping off at `game_tipoff`, using only games known at as_of."""
    prior = store.results(as_of, team=team)
    if not prior:
        return RestInfo(None, False, 0)
    game_day = game_tipoff.astimezone(ET).date()
    last_day = prior[-1].tipoff_utc.astimezone(ET).date()
    gap = (game_day - last_day).days
    last7 = sum(1 for r in prior if 0 < (game_day - r.tipoff_utc.astimezone(ET).date()).days <= 7)
    return RestInfo(days_rest=gap - 1, back_to_back=gap == 1, games_last_7_days=last7)


def head_to_head(store: DataStore, as_of: datetime, team_a: str, team_b: str, last_n: int):
    """Past meetings between two teams, oldest first."""
    rows = store.results(as_of, team=team_a, opponent=team_b)
    return rows[-last_n:] if last_n > 0 else []
