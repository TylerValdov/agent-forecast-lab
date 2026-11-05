"""Pre-game feature vector for ML baselines. Computed from the DataStore at the game's own
`as_of`, so a feature row never contains information from after that game's prediction time.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from forecast_lab.data.derived import form_summary, head_to_head, rest_info
from forecast_lab.data.schema import ScheduledGame
from forecast_lab.data.store import DataStore

FEATURE_NAMES = [
    "net_rating_l10_diff",
    "margin_l10_diff",
    "win_pct_l10_diff",
    "season_win_pct_diff",
    "home_rest",
    "away_rest",
    "home_b2b",
    "away_b2b",
    "home_games_l7",
    "away_games_l7",
    "h2h_home_win_frac_l3",
]


def game_features(store: DataStore, game: ScheduledGame, as_of: datetime) -> np.ndarray:
    h = form_summary(store, as_of, game.home, 10, season=game.season)
    a = form_summary(store, as_of, game.away, 10, season=game.season)
    hr = rest_info(store, as_of, game.home, game.tipoff_utc)
    ar = rest_info(store, as_of, game.away, game.tipoff_utc)
    meetings = head_to_head(store, as_of, game.home, game.away, 3)
    h2h = (sum(1 for r in meetings if (r.home == game.home) == r.home_win) / len(meetings)
           if meetings else 0.5)

    def rest(x):
        return 3.0 if x.days_rest is None else float(min(x.days_rest, 3))

    return np.array([
        (h.avg_net_rating or 0.0) - (a.avg_net_rating or 0.0),
        h.avg_margin - a.avg_margin,
        h.win_pct - a.win_pct,
        h.season_win_pct - a.season_win_pct,
        rest(hr),
        rest(ar),
        float(hr.back_to_back),
        float(ar.back_to_back),
        float(hr.games_last_7_days),
        float(ar.games_last_7_days),
        h2h,
    ])
