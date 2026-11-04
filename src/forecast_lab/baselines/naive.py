"""Trivial reference forecasters."""

from __future__ import annotations

from collections import deque

from forecast_lab.data.schema import GameResult


class AlwaysHalf:
    name = "always_0.5"

    def predict(self, store, game, as_of) -> float:
        return 0.5

    def observe(self, result: GameResult) -> None:
        pass


class HomeRate:
    """Rolling home-win base rate over the last `window` finished games, with a weak
    Beta(prior_strength/2, prior_strength/2) prior toward 0.5."""

    def __init__(self, window: int = 1000, prior_strength: float = 20.0) -> None:
        self.name = "home_rate"
        self._recent: deque[int] = deque(maxlen=window)
        self._prior = prior_strength

    def predict(self, store, game, as_of) -> float:
        n = len(self._recent)
        return (sum(self._recent) + self._prior / 2) / (n + self._prior)

    def observe(self, result: GameResult) -> None:
        self._recent.append(int(result.home_win))
