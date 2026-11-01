"""Typed tools exposed to agents. Each call is bound to one game and one `as_of`, routes
through the DataStore, and passes its output through the Masker.

Tool outputs use relative time ("days_ago") rather than calendar dates, so the same code
serves raw and anonymized modes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from forecast_lab.data.derived import ET, form_summary, head_to_head, rest_info
from forecast_lab.data.masker import Masker
from forecast_lab.data.schema import ScheduledGame
from forecast_lab.data.store import DataStore

PROB_MIN, PROB_MAX = 0.05, 0.95


class ToolError(Exception):
    """A recoverable tool error, returned to the agent as an is_error tool_result."""


class Forecast(BaseModel):
    prob_home_win: float = Field(ge=PROB_MIN, le=PROB_MAX)
    reasoning: str = Field(min_length=1, max_length=4000)


def _strict_tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_TEAM = {"type": "string", "description": "Team identifier exactly as given in the task."}
_N = {"type": "integer", "description": "Number of most recent games (1-20)."}

SUBMIT_FORECAST = _strict_tool(
    "submit_forecast",
    "Submit your final forecast. This is the only way to answer. "
    f"prob_home_win must be between {PROB_MIN} and {PROB_MAX}.",
    {
        "prob_home_win": {"type": "number",
                          "description": "Probability that the HOME team wins."},
        "reasoning": {"type": "string", "description": "Brief justification (<150 words)."},
    },
    ["prob_home_win", "reasoning"],
)

DATA_TOOLS = {
    "get_team_form": _strict_tool(
        "get_team_form",
        "A team's most recent completed games (result, margin, opponent, home/away, days ago) "
        "plus summary stats: record, average point margin, average net rating, season record.",
        {"team": _TEAM, "last_n": _N}, ["team", "last_n"],
    ),
    "get_rest_days": _strict_tool(
        "get_rest_days",
        "Days of rest before this game, back-to-back flag, and games played in the last 7 days.",
        {"team": _TEAM}, ["team"],
    ),
    "get_injuries": _strict_tool(
        "get_injuries",
        "The latest official injury report for a team published before now.",
        {"team": _TEAM}, ["team"],
    ),
    "get_head_to_head": _strict_tool(
        "get_head_to_head",
        "Previous meetings between the two teams (most recent last), across seasons.",
        {"team_a": _TEAM, "team_b": _TEAM, "last_n": _N}, ["team_a", "team_b", "last_n"],
    ),
}


@dataclass
class NBAToolbox:
    """All data tools for one (game, as_of). Construct a fresh one per prediction."""

    store: DataStore
    game: ScheduledGame
    as_of: datetime
    masker: Masker
    injuries_enabled: bool = True
    calls: list[dict] = field(default_factory=list)

    def specs(self, extra: list[dict] | None = None) -> list[dict]:
        names = [n for n in DATA_TOOLS if n != "get_injuries" or self.injuries_enabled]
        return [DATA_TOOLS[n] for n in names] + [SUBMIT_FORECAST] + (extra or [])

    # ------------------------------------------------------------------ helpers
    def label(self, tricode: str) -> str:
        return self.masker.team(tricode)

    def _resolve(self, label: str) -> str:
        for tri in (self.game.home, self.game.away):
            if label in (tri, self.label(tri)):
                return tri
        raise ToolError(
            f"Unknown team '{label}'. Use one of: {self.label(self.game.home)}, "
            f"{self.label(self.game.away)}."
        )

    @staticmethod
    def _n(n: Any) -> int:
        if not isinstance(n, int) or not 1 <= n <= 20:
            raise ToolError("last_n must be an integer between 1 and 20.")
        return n

    def _days_ago(self, t: datetime) -> int:
        return (self.as_of.astimezone(ET).date() - t.astimezone(ET).date()).days

    # -------------------------------------------------------------------- tools
    def get_team_form(self, team: str, last_n: int) -> dict:
        tri, n = self._resolve(team), self._n(last_n)
        games = self.store.results(self.as_of, team=tri, last_n=n)
        s = form_summary(self.store, self.as_of, tri, n, season=self.game.season)
        rows = []
        for r in games:
            home = r.home == tri
            margin = (r.home_pts - r.away_pts) * (1 if home else -1)
            own, opp = (r.home_pts, r.away_pts) if home else (r.away_pts, r.home_pts)
            rows.append({
                "days_ago": self._days_ago(r.tipoff_utc),
                "opponent": self.label(r.away if home else r.home),
                "venue": "home" if home else "away",
                "result": "W" if margin > 0 else "L",
                "score": f"{own}-{opp}",
                "margin": margin,
            })
        return {
            "team": self.label(tri),
            "games": rows,
            "summary": {
                "record": f"{s.wins}-{s.games - s.wins}",
                "avg_margin": round(s.avg_margin, 1),
                "avg_net_rating": None if s.avg_net_rating is None else round(s.avg_net_rating, 1),
                "season_record": f"{s.season_wins}-{s.season_games - s.season_wins}",
            },
        }

    def get_rest_days(self, team: str) -> dict:
        tri = self._resolve(team)
        info = rest_info(self.store, self.as_of, tri, self.game.tipoff_utc)
        return {
            "team": self.label(tri),
            "days_rest": info.days_rest,
            "back_to_back": info.back_to_back,
            "games_last_7_days": info.games_last_7_days,
            "note": "days_rest is null if the team has no prior game in the data"
            if info.days_rest is None else "",
        }

    def get_injuries(self, team: str) -> dict:
        if not self.injuries_enabled:
            raise ToolError("Injury data is not available in this mode.")
        tri = self._resolve(team)
        snap = self.store.latest_injury_report(self.as_of, tri)
        if snap is None:
            return {"team": self.label(tri), "report": None,
                    "note": "No injury report published before now."}
        hours = (self.as_of - snap.published_at).total_seconds() / 3600
        return {
            "team": self.label(tri),
            "published_hours_ago": round(hours, 1),
            "entries": [{"player": self.masker.player(e.player), "status": e.status,
                         "reason": self.masker.text(e.reason)} for e in snap.entries],
        }

    def get_head_to_head(self, team_a: str, team_b: str, last_n: int) -> dict:
        a, b, n = self._resolve(team_a), self._resolve(team_b), self._n(last_n)
        if a == b:
            raise ToolError("team_a and team_b must differ.")
        rows = []
        for r in head_to_head(self.store, self.as_of, a, b, n):
            winner = r.home if r.home_win else r.away
            rows.append({
                "days_ago": self._days_ago(r.tipoff_utc),
                "home": self.label(r.home),
                "winner": self.label(winner),
                "score": f"{self.label(r.home)} {r.home_pts} - {r.away_pts} {self.label(r.away)}",
            })
        return {"meetings": rows}

    # ------------------------------------------------------------------ dispatch
    def execute(self, name: str, args: dict) -> tuple[str, bool]:
        """Run a data tool. Returns (json_text, is_error). Never raises for agent errors."""
        fn = {
            "get_team_form": self.get_team_form,
            "get_rest_days": self.get_rest_days,
            "get_injuries": self.get_injuries,
            "get_head_to_head": self.get_head_to_head,
        }.get(name)
        try:
            if fn is None:
                raise ToolError(f"Unknown tool '{name}'.")
            out = json.dumps(fn(**args))
            is_error = False
        except ToolError as e:
            out, is_error = json.dumps({"error": str(e)}), True
        except TypeError as e:
            out, is_error = json.dumps({"error": f"Bad arguments: {e}"}), True
        self.calls.append({"name": name, "args": args, "is_error": is_error})
        return out, is_error


def parse_forecast(args: dict) -> tuple[Forecast | None, str | None]:
    try:
        return Forecast(**args), None
    except (ValidationError, TypeError) as e:
        return None, f"Invalid forecast: {e}. prob_home_win must be in [{PROB_MIN}, {PROB_MAX}]."
