"""Shared fixtures. ALL DATA HERE IS SYNTHETIC and exists only for unit tests."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from forecast_lab.data.derived import ET
from forecast_lab.data.schema import GameResult, ScheduledGame, TeamBox
from forecast_lab.data.store import DataStore

DELAY = timedelta(minutes=210)
TEAMS = ["BOS", "NYK", "LAL", "MIA", "DEN", "PHX"]


def make_result(gid: str, tip: datetime, home: str, away: str, hp: int, ap: int,
                season: str = "2030-31") -> GameResult:
    return GameResult(game_id=gid, season=season, game_date=tip.astimezone(ET).date(),
                      tipoff_utc=tip, home=home, away=away, home_pts=hp, away_pts=ap,
                      available_at=tip + DELAY)


def box_for(r: GameResult) -> list[TeamBox]:
    return [
        TeamBox(game_id=r.game_id, team=t, opponent=o, is_home=h, tipoff_utc=r.tipoff_utc,
                pts=p, opp_pts=op, possessions=100.0, available_at=r.available_at)
        for t, o, h, p, op in [(r.home, r.away, True, r.home_pts, r.away_pts),
                               (r.away, r.home, False, r.away_pts, r.home_pts)]
    ]


def synthetic_league(n_days: int = 120, seed: int = 0) -> tuple[list[GameResult], list]:
    """Round-robin-ish synthetic season: 3 games a night at 00:00 UTC, with team strength
    differences so that baselines have signal to find."""
    rng = random.Random(seed)
    strength = {t: s for t, s in zip(TEAMS, [6, 3, 1, -1, -3, -6], strict=True)}
    start = datetime(2030, 11, 1, 0, 0, tzinfo=UTC)
    results = []
    gid = 0
    for d in range(n_days):
        teams = TEAMS[:]
        rng.shuffle(teams)
        for i in range(0, 6, 2):
            home, away = teams[i], teams[i + 1]
            tip = start + timedelta(days=d, minutes=30 * i)
            margin = strength[home] - strength[away] + 3 + rng.gauss(0, 12)
            hp = 110 + round(margin / 2)
            ap = 110 - round(margin / 2) - (1 if round(margin / 2) * 2 == 0 else 0)
            if hp == ap:
                hp += 1
            gid += 1
            results.append(make_result(f"S{gid:05d}", tip, home, away, hp, ap))
    return results, [b for r in results for b in box_for(r)]


@pytest.fixture
def league_store() -> DataStore:
    results, box = synthetic_league()
    return DataStore(results=results, schedule=[], box=box, source="synthetic-test")


@pytest.fixture
def tiny_store() -> DataStore:
    """Hand-built scenario for leakage tests (synthetic).

    Day 1 (UTC): G1 BOS@NYK 17:00 (avail 20:30), G2 LAL@MIA 23:00 (avail 02:30 next day),
    and G3 MIA@BOS 23:00 is the game we predict, with an earlier BOS-MIA meeting G0.
    """
    d = datetime(2030, 1, 10, tzinfo=UTC)
    g0 = make_result("G0", d - timedelta(days=5) + timedelta(hours=23), "BOS", "MIA", 101, 99)
    g1 = make_result("G1", d + timedelta(hours=17), "NYK", "BOS", 100, 90)
    g2 = make_result("G2", d + timedelta(hours=23), "MIA", "LAL", 120, 80)
    g3 = make_result("G3", d + timedelta(days=1, hours=23), "BOS", "MIA", 95, 130)
    future = ScheduledGame(game_id="G4", season="2030-31", game_date=(d + timedelta(days=3)).date(),
                           tipoff_utc=d + timedelta(days=3, hours=23), home="NYK", away="LAL")
    rs = [g0, g1, g2, g3]
    return DataStore(results=rs, schedule=[future], box=[b for r in rs for b in box_for(r)],
                     source="synthetic-test")
