"""Baselines (walk-forward, no future data) and evaluation statistics. (Synthetic data.)"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from forecast_lab.baselines.base import walk_forward
from forecast_lab.baselines.elo import Elo
from forecast_lab.baselines.market import devig_two_way, parse_odds_api
from forecast_lab.baselines.ml import WalkForwardML
from forecast_lab.baselines.naive import AlwaysHalf, HomeRate
from forecast_lab.eval.metrics import brier, brier_decomposition, ece, log_loss
from forecast_lab.eval.stats import benjamini_hochberg, holm, paired_bootstrap

FAR = datetime(2100, 1, 1, tzinfo=UTC)


class Spy:
    """Records what it observed relative to each prediction time."""

    name = "spy"

    def __init__(self):
        self.observed = []
        self.violations = 0

    def observe(self, r):
        self.observed.append(r)

    def predict(self, store, game, as_of):
        self.violations += sum(1 for r in self.observed if r.available_at >= as_of)
        self.violations += any(r.game_id == game.game_id for r in self.observed)
        return 0.5


def games_of(store):
    return [r.to_scheduled() for r in store.results(FAR)]


def test_walk_forward_never_feeds_future_results(league_store):
    spy = Spy()
    walk_forward(league_store, games_of(league_store), [spy])
    assert spy.violations == 0 and len(spy.observed) > 0


def test_ml_training_windows_are_strictly_past(league_store):
    ml = WalkForwardML("logistic", retrain_days=7, min_train=60)
    preds = walk_forward(league_store, games_of(league_store), [ml])
    assert ml.fit_log, "model never trained"
    by_time = {p.as_of for p in preds}
    for fit in ml.fit_log:
        assert fit.max_label_available_at < fit.fit_as_of
        assert fit.fit_as_of in by_time
    # Every prediction after a fit used a model whose labels all predate the prediction.
    fits = sorted(f.fit_as_of for f in ml.fit_log)
    for p in preds:
        if p.prob is not None:
            assert any(f <= p.as_of for f in fits)


def test_baselines_beat_coin_flip_on_synthetic_signal(league_store):
    games = games_of(league_store)
    fs = [AlwaysHalf(), HomeRate(), Elo(), WalkForwardML("logistic", min_train=60)]
    preds = walk_forward(league_store, games, fs)
    y = {g.game_id: int(league_store.reveal(g.game_id, FAR).home_win) for g in games}
    late = {g.game_id for g in games[200:]}

    def score(name):
        ps = [(p.prob, y[p.game_id]) for p in preds
              if p.model == name and p.game_id in late and p.prob is not None]
        p, t = np.array(ps).T
        return brier(p, t).mean()

    assert score("elo") < score("always_0.5") - 0.01
    assert score("logistic") < score("always_0.5") - 0.01


def test_elo_season_regression():
    from tests.conftest import make_result

    e = Elo()
    t = datetime(2030, 1, 1, tzinfo=UTC)
    for i in range(20):
        e.observe(make_result(f"X{i}", t + timedelta(days=i), "BOS", "NYK", 120, 90))
    before = e.ratings["BOS"]
    e.observe(make_result("Y", t + timedelta(days=300), "BOS", "NYK", 100, 99, season="2031-32"))
    assert abs(e.ratings["BOS"] - 1505) < abs(before - 1505)


def test_metrics_known_values():
    p, y = np.array([0.8, 0.3]), np.array([1, 0])
    assert np.allclose(brier(p, y), [0.04, 0.09])
    assert np.isclose(log_loss(p, y).mean(), -(np.log(0.8) + np.log(0.7)) / 2)
    perfect = np.repeat([0.2, 0.8], 50)
    outcomes = np.r_[np.r_[np.ones(10), np.zeros(40)], np.r_[np.ones(40), np.zeros(10)]]
    assert ece(perfect, outcomes) < 1e-9
    d = brier_decomposition(perfect, outcomes)
    assert abs(d.residual) < 1e-9 and d.reliability < 1e-9


def test_paired_bootstrap_detects_real_difference_and_not_noise():
    rng = np.random.default_rng(0)
    a = rng.uniform(0.1, 0.3, 2000)
    r = paired_bootstrap(a - 0.01, a, n_resamples=2000)
    assert r.ci_high < 0 and r.p_value < 0.01
    r = paired_bootstrap(a + rng.normal(0, 0.05, 2000), a, n_resamples=2000)
    assert r.ci_low < 0 < r.ci_high


def test_multiple_comparison_corrections():
    p = [0.01, 0.04, 0.03, 0.5]
    assert np.allclose(holm(p), [0.04, 0.09, 0.09, 0.5])
    assert np.allclose(benjamini_hochberg(p), [0.04, 0.04 * 4 / 3, 0.04 * 4 / 3, 0.5])


def test_market_devig_and_parse():
    assert np.isclose(devig_two_way(1.5, 2.6), (1 / 1.5) / (1 / 1.5 + 1 / 2.6))
    from forecast_lab.data.schema import ScheduledGame

    tip = datetime(2030, 1, 2, 0, 30, tzinfo=UTC)
    g = ScheduledGame(game_id="G", season="2029-30", game_date=tip.date(), tipoff_utc=tip,
                      home="BOS", away="NYK")
    # Synthetic Odds-API-shaped payload (test fixture, not real odds).
    ev = [{"home_team": "Boston Celtics", "away_team": "New York Knicks",
           "commence_time": "2030-01-02T00:30:00Z",
           "bookmakers": [{"markets": [{"key": "h2h", "outcomes": [
               {"name": "Boston Celtics", "price": 1.5},
               {"name": "New York Knicks", "price": 2.6}]}]}]}]
    q = parse_odds_api(ev, [g], tip)["G"]
    assert np.isclose(q.prob_home, devig_two_way(1.5, 2.6)) and q.n_books == 1
