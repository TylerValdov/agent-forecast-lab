"""Recall probe (contamination audit, RQ5).

With no tools, the model is asked for the exact final score of real games, named with real
teams and dates. Exact scores are nearly impossible to guess, so recall well above the
post-cutoff control rate is direct evidence of memorized outcomes. Winner accuracy alone is
NOT evidence (a model can pick favourites from general knowledge), so it is reported
alongside a "pick the better-record team" heuristic, not as a recall measure.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time

from forecast_lab.agents import prompts
from forecast_lab.agents.llm import LLMClient, LLMError, cost_usd
from forecast_lab.data.masker import NBA_TEAMS
from forecast_lab.data.schema import GameResult
from forecast_lab.data.store import DataStore

FAR = datetime(2100, 1, 1, tzinfo=UTC)


def _full(tri: str) -> str:
    city, nick = NBA_TEAMS[tri]
    return f"{city} {nick}"


def question(g: GameResult) -> str:
    return (f"NBA regular-season game on {g.game_date.isoformat()} (US Eastern date): "
            f"{_full(g.away)} (away) at {_full(g.home)} (home). What was the final score?")


_JSON = re.compile(r"\{.*?\}", re.S)


def parse_answer(text: str) -> dict | None:
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return {"home_pts": int(d["home_pts"]), "away_pts": int(d["away_pts"]),
                "remembered": bool(d.get("remembered", False))}
    except (ValueError, KeyError, TypeError):
        return None


def score(ans: dict | None, g: GameResult) -> dict:
    if ans is None:
        return {"parsed": False}
    true_margin = g.home_pts - g.away_pts
    margin = ans["home_pts"] - ans["away_pts"]
    return {
        "parsed": True,
        "exact": ans["home_pts"] == g.home_pts and ans["away_pts"] == g.away_pts,
        "within3_both": abs(ans["home_pts"] - g.home_pts) <= 3
        and abs(ans["away_pts"] - g.away_pts) <= 3,
        "winner_correct": (margin > 0) == (true_margin > 0) and margin != 0,
        "margin_abs_err": abs(margin - true_margin),
        "remembered": ans["remembered"],
    }


@dataclass(frozen=True)
class ProbeItem:
    game: GameResult
    group: str  # "pre" (experiment games) or "post" (after training cutoff)


def probe_items(games: list[GameResult], post_store: DataStore | None, n_pre: int,
                n_post: int, post_after, seed: int, now: datetime) -> list[ProbeItem]:
    rng = random.Random(seed)
    pre = sorted(games, key=lambda g: g.game_id)
    pre = rng.sample(pre, min(n_pre, len(pre)))
    items = [ProbeItem(g, "pre") for g in pre]
    if post_store is not None and n_post > 0:
        cutoff = datetime.combine(post_after, time(), tzinfo=UTC)
        post = sorted((g for g in post_store.results(now) if g.tipoff_utc >= cutoff),
                      key=lambda g: g.game_id)
        items += [ProbeItem(g, "post") for g in rng.sample(post, min(n_post, len(post)))]
    return items


def ask(llm: LLMClient, item: ProbeItem, prompt_version: str = "v1") -> dict:
    g = item.game
    q = question(g)
    try:
        resp = llm.create(system=prompts.load("recall_probe", prompt_version),
                          messages=[{"role": "user", "content": q}], tools=[],
                          max_tokens=2000)
    except LLMError as e:
        return {"kind": "probe", "game_id": g.game_id, "group": item.group, "question": q,
                "error": str(e), "cost_usd": 0.0, "tokens": {}}
    text = " ".join(b.get("text", "") for b in resp.content if b.get("type") == "text")
    ans = parse_answer(text)
    return {
        "kind": "probe", "game_id": g.game_id, "group": item.group,
        "game_date": g.game_date.isoformat(), "season": g.season, "question": q,
        "answer_text": text[:500], "answer": ans,
        "truth": {"home_pts": g.home_pts, "away_pts": g.away_pts},
        **score(ans, g),
        "model": llm.model, "cost_usd": cost_usd(llm.model, resp.usage),
        "tokens": resp.usage.to_dict(), "timestamp": datetime.now(UTC).isoformat(),
    }
