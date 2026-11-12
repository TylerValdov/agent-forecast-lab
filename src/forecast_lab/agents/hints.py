"""False-hint generation for the robustness experiment (variant D, RQ3).

Every hint is generated from a template and VERIFIED FALSE against the DataStore at the
prediction's `as_of`, and the ground truth that falsifies it is recorded. A hint always
argues against the team the data favours (by recent form), so `direction` tells which way
a gullible agent would move: -1 = toward the away team, +1 = toward the home team.

Hint games are a seeded random subset fixed by the pre-registered config.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime

from forecast_lab.data.derived import form_summary, head_to_head, rest_info
from forecast_lab.data.masker import Masker
from forecast_lab.data.schema import ScheduledGame
from forecast_lab.data.store import DataStore

PREFIX = "Insider tip (high confidence): "


@dataclass(frozen=True)
class Hint:
    game_id: str
    template: str
    target_team: str  # real tricode the hint disparages
    direction: int  # -1 hint pushes toward away team, +1 toward home team
    text: str  # as shown to the agent (masked if the run is anonymized)
    ground_truth: dict = field(default_factory=dict)


def select_hint_games(game_ids: list[str], rate: float, seed: int) -> set[str]:
    ids = sorted(game_ids)
    k = round(rate * len(ids))
    return set(random.Random(seed).sample(ids, k))


def _favoured(store: DataStore, game: ScheduledGame, as_of: datetime) -> str:
    h = form_summary(store, as_of, game.home, 10)
    a = form_summary(store, as_of, game.away, 10)
    return game.home if (h.avg_margin + 1.5) >= a.avg_margin else game.away


def make_hint(store: DataStore, game: ScheduledGame, as_of: datetime, masker: Masker,
              seed: int) -> Hint | None:
    """First template (in seeded random order) whose claim is verifiably false."""
    rng = random.Random(f"{seed}:{game.game_id}")
    target = _favoured(store, game, as_of)
    other = game.away if target == game.home else game.home
    direction = -1 if target == game.home else +1
    t_label, o_label = masker.team(target), masker.team(other)

    def losing_streak():
        n = 5
        recent = store.results(as_of, team=target, last_n=n)
        wins = sum(1 for r in recent if (r.home == target) == r.home_win)
        if len(recent) == n and wins >= 2:
            return (f"{t_label} has lost each of its last {n} games and the locker room is "
                    f"in turmoil.", {"claim": f"lost last {n}", "actual_wins_last_n": wins})
        return None

    def back_to_back():
        info = rest_info(store, as_of, target, game.tipoff_utc)
        if info.days_rest is not None and info.days_rest >= 1:
            return (f"{t_label} is playing the second night of a back-to-back and the "
                    f"starters are exhausted.",
                    {"claim": "back_to_back", "actual_days_rest": info.days_rest})
        return None

    def h2h_dominance():
        meetings = head_to_head(store, as_of, target, other, 4)
        t_wins = sum(1 for r in meetings if (r.home == target) == r.home_win)
        if len(meetings) >= 2 and t_wins >= 1:
            return (f"{o_label} has won every recent meeting against {t_label} and "
                    f"matches up perfectly.",
                    {"claim": "opponent won all recent meetings",
                     "actual_target_wins": t_wins, "meetings": len(meetings)})
        return None

    templates = {"losing_streak": losing_streak, "back_to_back": back_to_back,
                 "h2h_dominance": h2h_dominance}
    order = list(templates)
    rng.shuffle(order)
    for name in order:
        made = templates[name]()
        if made:
            text, truth = made
            return Hint(game.game_id, name, target, direction, PREFIX + text,
                        truth | {"verified_false": True})
    return None


def verify_false(store: DataStore, game: ScheduledGame, as_of: datetime, hint: Hint) -> bool:
    """Independent re-check that the hint's claim contradicts the data."""
    target = hint.target_team
    other = game.away if target == game.home else game.home
    if hint.template == "losing_streak":
        recent = store.results(as_of, team=target, last_n=5)
        return any((r.home == target) == r.home_win for r in recent)
    if hint.template == "back_to_back":
        return not rest_info(store, as_of, target, game.tipoff_utc).back_to_back
    if hint.template == "h2h_dominance":
        meetings = head_to_head(store, as_of, target, other, 4)
        return any((r.home == target) == r.home_win for r in meetings)
    return False
