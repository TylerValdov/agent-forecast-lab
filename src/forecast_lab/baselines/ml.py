"""Walk-forward ML baselines: logistic regression and gradient boosting.

* Refit every `retrain_days` using only games whose results were `observe()`d (i.e. were
  available before the current `as_of`).
* Hyperparameters are chosen at each refit by time-ordered cross-validation INSIDE the
  training window (sklearn TimeSeriesSplit), scored by Brier.
* Every fit is recorded in `fit_log` (fit time, n, latest label availability) so a test can
  prove no training window ever reached past the prediction time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GridSearchCV, TimeSeriesSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from forecast_lab.baselines.features import game_features
from forecast_lab.data.schema import GameResult, ScheduledGame
from forecast_lab.data.store import DataStore


@dataclass(frozen=True)
class FitRecord:
    fit_as_of: datetime
    n_train: int
    max_label_available_at: datetime
    params: dict


def _estimator(kind: str):
    if kind == "logistic":
        return (make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000)),
                {"logisticregression__C": [0.01, 0.1, 1.0]})
    if kind == "gbm":
        return (HistGradientBoostingClassifier(learning_rate=0.05, random_state=0),
                {"max_depth": [2, 3], "max_iter": [100, 200]})
    raise ValueError(kind)


class WalkForwardML:
    def __init__(self, kind: str = "logistic", retrain_days: int = 14, min_train: int = 400,
                 lead: timedelta = timedelta(hours=1), cv_splits: int = 4) -> None:
        self.name = kind
        self.kind = kind
        self.retrain = timedelta(days=retrain_days)
        self.min_train = min_train
        self.lead = lead
        self.cv_splits = cv_splits
        self.fit_log: list[FitRecord] = []
        self._x: dict[str, np.ndarray] = {}
        self._labels: list[tuple[datetime, str, int, datetime]] = []  # (tip, id, y, avail)
        self._model = None
        self._last_fit: datetime | None = None
        self._store: DataStore | None = None

    def _features(self, store: DataStore, game: ScheduledGame) -> np.ndarray:
        if game.game_id not in self._x:
            # Always computed at the game's OWN prediction time, never later.
            self._x[game.game_id] = game_features(store, game, game.tipoff_utc - self.lead)
        return self._x[game.game_id]

    def bind(self, store: DataStore) -> None:
        """Attach the store before observing results outside walk_forward (live mode)."""
        self._store = store

    def observe(self, result: GameResult) -> None:
        if self._store is not None:
            self._features(self._store, result.to_scheduled())
        self._labels.append((result.tipoff_utc, result.game_id, int(result.home_win),
                             result.available_at))

    def _fit(self, as_of: datetime) -> None:
        rows = sorted(r for r in self._labels if r[1] in self._x)
        assert all(r[3] < as_of for r in rows), "training label from the future"
        x = np.stack([self._x[r[1]] for r in rows])
        y = np.array([r[2] for r in rows])
        est, grid = _estimator(self.kind)
        search = GridSearchCV(est, grid, cv=TimeSeriesSplit(self.cv_splits),
                              scoring="neg_brier_score")
        search.fit(x, y)
        self._model = search.best_estimator_
        self._last_fit = as_of
        self.fit_log.append(FitRecord(as_of, len(rows), max(r[3] for r in rows),
                                      dict(search.best_params_)))

    def predict(self, store: DataStore, game: ScheduledGame, as_of: datetime) -> float | None:
        self._store = store
        x = self._features(store, game)
        if len(self._labels) < self.min_train:
            return None
        if self._last_fit is None or as_of - self._last_fit >= self.retrain:
            self._fit(as_of)
        return float(self._model.predict_proba(x.reshape(1, -1))[0, 1])
