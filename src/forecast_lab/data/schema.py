"""Typed records. Every record that carries information about the past has an
availability timestamp; the DataStore uses it to enforce the as_of guard.

All timestamps are `AwareDatetime`: naive datetimes are rejected at construction,
because a naive timestamp is the most common way a leakage bug sneaks in."""

from __future__ import annotations

from datetime import date

from pydantic import AwareDatetime, BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScheduledGame(_Frozen):
    """What is publicly known about a game before it is played. Contains no outcome."""

    game_id: str
    season: str
    game_date: date  # local (US/Eastern) calendar date
    tipoff_utc: AwareDatetime
    home: str  # team tricode, e.g. "BOS"
    away: str


class GameResult(_Frozen):
    """A finished game. Only returned by the DataStore once `available_at < as_of`."""

    game_id: str
    season: str
    game_date: date
    tipoff_utc: AwareDatetime
    home: str
    away: str
    home_pts: int
    away_pts: int
    available_at: AwareDatetime

    @property
    def home_win(self) -> bool:
        return self.home_pts > self.away_pts

    def to_scheduled(self) -> ScheduledGame:
        return ScheduledGame(
            game_id=self.game_id,
            season=self.season,
            game_date=self.game_date,
            tipoff_utc=self.tipoff_utc,
            home=self.home,
            away=self.away,
        )


class TeamBox(_Frozen):
    """One team's box-score line for one finished game."""

    game_id: str
    team: str
    opponent: str
    is_home: bool
    tipoff_utc: AwareDatetime
    pts: int
    opp_pts: int
    possessions: float  # estimated: FGA + 0.44*FTA - OREB + TOV
    available_at: AwareDatetime

    @property
    def net_rating(self) -> float:
        return 100.0 * (self.pts - self.opp_pts) / max(self.possessions, 1.0)


class InjuryEntry(_Frozen):
    player: str
    status: str  # e.g. "Out", "Questionable", "Available"
    reason: str = ""


class InjurySnapshot(_Frozen):
    """An official injury report for one team, as published at `published_at`."""

    team: str
    published_at: AwareDatetime
    entries: tuple[InjuryEntry, ...]
    source: str
