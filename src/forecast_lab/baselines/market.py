"""Market baseline: implied home-win probability from betting lines, vig removed.

There is no free, reliable historical NBA odds source available to this project, so in
replay modes the market baseline is MISSING (not approximated). For `live` mode it can be
pulled from The Odds API (https://the-odds-api.com) with an optional ODDS_API_KEY.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Protocol

import requests

from forecast_lab.data.masker import NBA_TEAMS
from forecast_lab.data.schema import ScheduledGame

FULL_NAME_TO_TRI = {f"{c} {n}": t for t, (c, n) in NBA_TEAMS.items()}
FULL_NAME_TO_TRI["LA Clippers"] = "LAC"


@dataclass(frozen=True)
class MarketQuote:
    game_id: str
    prob_home: float
    n_books: int
    fetched_at: datetime
    source: str


class MarketSource(Protocol):
    available: bool
    reason: str

    def quotes(self, games: list[ScheduledGame], now: datetime) -> dict[str, MarketQuote]: ...


class NoMarket:
    available = False

    def __init__(self, reason: str = "no historical odds source; not approximated") -> None:
        self.reason = reason

    def quotes(self, games, now):
        return {}


def devig_two_way(price_home: float, price_away: float) -> float:
    """Decimal odds -> vig-free home probability (multiplicative normalization)."""
    ph, pa = 1.0 / price_home, 1.0 / price_away
    return ph / (ph + pa)


def parse_odds_api(events: list[dict], games: list[ScheduledGame], now: datetime,
                   ) -> dict[str, MarketQuote]:
    by_key = {(g.home, g.away, g.tipoff_utc.date()): g for g in games}
    out: dict[str, MarketQuote] = {}
    for ev in events:
        home = FULL_NAME_TO_TRI.get(ev.get("home_team", ""))
        away = FULL_NAME_TO_TRI.get(ev.get("away_team", ""))
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        game = by_key.get((home, away, start.date()))
        if game is None:
            continue
        probs = []
        for book in ev.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                prices = {o["name"]: o["price"] for o in market.get("outcomes", [])}
                if ev["home_team"] in prices and ev["away_team"] in prices:
                    probs.append(devig_two_way(prices[ev["home_team"]], prices[ev["away_team"]]))
        if probs:
            out[game.game_id] = MarketQuote(game.game_id, median(probs), len(probs), now,
                                            "the-odds-api:h2h:median-of-books")
    return out


class OddsAPIMarket:
    URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("ODDS_API_KEY", "")
        self.available = bool(self.api_key)
        self.reason = "" if self.available else "ODDS_API_KEY not set"

    def quotes(self, games, now):
        if not self.available:
            return {}
        resp = requests.get(self.URL, timeout=30, params={
            "apiKey": self.api_key, "regions": "us", "markets": "h2h", "oddsFormat": "decimal",
        })
        resp.raise_for_status()
        return parse_odds_api(resp.json(), games, now)
