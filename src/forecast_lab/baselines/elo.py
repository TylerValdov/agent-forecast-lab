"""Elo with home advantage and a margin-of-victory multiplier.

Hyperparameters follow FiveThirtyEight's published NBA Elo (fixed a priori, NOT tuned on
this data, so there is no hyperparameter leakage):
  K = 20, home advantage = 100 Elo points, initial rating = 1500,
  between seasons each rating regresses 25% toward 1505,
  MOV multiplier = (|MOV| + 3)^0.8 / (7.5 + 0.006 * elo_diff_of_winner).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from forecast_lab.data.schema import GameResult


@dataclass
class EloParams:
    k: float = 20.0
    home_adv: float = 100.0
    initial: float = 1500.0
    carryover: float = 0.75
    season_mean: float = 1505.0


@dataclass
class Elo:
    params: EloParams = field(default_factory=EloParams)
    name: str = "elo"

    def __post_init__(self) -> None:
        self.ratings: dict[str, float] = {}
        self._season: str | None = None

    def _rating(self, team: str) -> float:
        return self.ratings.setdefault(team, self.params.initial)

    def _roll_season(self, season: str) -> None:
        if self._season is not None and season > self._season:  # "2023-24" > "2022-23"
            p = self.params
            self.ratings = {t: p.carryover * r + (1 - p.carryover) * p.season_mean
                            for t, r in self.ratings.items()}
        if self._season is None or season > self._season:
            self._season = season

    def prob(self, home: str, away: str) -> float:
        diff = self._rating(home) + self.params.home_adv - self._rating(away)
        return 1.0 / (1.0 + 10 ** (-diff / 400.0))

    def predict(self, store, game, as_of) -> float:
        self._roll_season(game.season)
        return self.prob(game.home, game.away)

    def observe(self, r: GameResult) -> None:
        self._roll_season(r.season)
        p_home = self.prob(r.home, r.away)
        mov = abs(r.home_pts - r.away_pts)
        diff = self._rating(r.home) + self.params.home_adv - self._rating(r.away)
        winner_diff = diff if r.home_win else -diff
        mult = (mov + 3) ** 0.8 / (7.5 + 0.006 * winner_diff)
        delta = self.params.k * mult * (float(r.home_win) - p_home)
        self.ratings[r.home] = self._rating(r.home) + delta
        self.ratings[r.away] = self._rating(r.away) - delta
