"""Fetch NBA data from stats.nba.com (via `nba_api`), cache raw responses, and build a
DataStore.

Raw responses are cached as CSV under `cache_dir`, each with a `.meta.json` sidecar that
records when it was fetched and from which endpoint. Rebuilding the store from the cache is
deterministic, and its content hash is recorded in every run.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from forecast_lab.data.schema import GameResult, ScheduledGame, TeamBox
from forecast_lab.data.store import DataStore

ET = ZoneInfo("America/New_York")
REGULAR_SEASON_PREFIX = "002"  # NBA game_id prefix for regular-season games
FINAL_STATUS = 3


@dataclass(frozen=True)
class DataConfig:
    cache_dir: Path
    seasons: list[str]
    result_delay_minutes: int = 210
    request_pause_s: float = 1.0

    @classmethod
    def load(cls, path: str | Path = "configs/data.yaml") -> DataConfig:
        raw = yaml.safe_load(Path(path).read_text())
        return cls(
            cache_dir=Path(raw["cache_dir"]),
            seasons=list(raw["seasons"]),
            result_delay_minutes=int(raw.get("result_delay_minutes", 210)),
            request_pause_s=float(raw.get("request_pause_s", 1.0)),
        )


def current_season(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    start = now.year if now.month >= 9 else now.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


# --------------------------------------------------------------------------- fetching

def _write_cache(df: pd.DataFrame, path: Path, endpoint: str, params: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    meta = {
        "endpoint": endpoint,
        "params": params,
        "fetched_at": datetime.now(UTC).isoformat(),
        "rows": len(df),
    }
    path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2))


def fetch_season(season: str, cfg: DataConfig, force: bool = False) -> None:
    """Download schedule (with tipoff times + scores) and team box logs for one season."""
    from nba_api.stats.endpoints import leaguegamefinder, scheduleleaguev2

    sched_path = cfg.cache_dir / f"schedule_{season}.csv"
    if force or not sched_path.exists():
        df = scheduleleaguev2.ScheduleLeagueV2(season=season, timeout=60).get_data_frames()[0]
        cols = [
            "gameId", "gameDateTimeUTC", "gameDateEst", "gameStatus", "postponedStatus",
            "isNeutral", "homeTeam_teamTricode", "awayTeam_teamTricode",
            "homeTeam_score", "awayTeam_score",
        ]
        _write_cache(df[[c for c in cols if c in df.columns]], sched_path,
                     "ScheduleLeagueV2", {"season": season})
        time.sleep(cfg.request_pause_s)

    logs_path = cfg.cache_dir / f"teamlogs_{season}.csv"
    if force or not logs_path.exists():
        df = leaguegamefinder.LeagueGameFinder(
            season_nullable=season,
            league_id_nullable="00",
            season_type_nullable="Regular Season",
            timeout=60,
        ).get_data_frames()[0]
        _write_cache(df, logs_path, "LeagueGameFinder",
                     {"season": season, "season_type": "Regular Season"})
        time.sleep(cfg.request_pause_s)


def refresh(cfg: DataConfig, seasons: list[str] | None = None, force: bool = False) -> None:
    for season in seasons or cfg.seasons:
        fetch_season(season, cfg, force=force)


# ---------------------------------------------------------------------------- parsing

def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def parse_schedule(df: pd.DataFrame, season: str, result_delay: timedelta):
    """-> (scheduled games, finished results). Regular season only."""
    df = df.copy()
    df["gameId"] = df["gameId"].astype(str).str.zfill(10)
    df = df[df["gameId"].str.startswith(REGULAR_SEASON_PREFIX)]
    scheduled: list[ScheduledGame] = []
    results: list[GameResult] = []
    for row in df.itertuples(index=False):
        if pd.isna(row.homeTeam_teamTricode) or pd.isna(row.awayTeam_teamTricode):
            continue  # placeholder slot (e.g. NBA Cup knockout game, teams not yet known)
        tip = _parse_utc(row.gameDateTimeUTC)
        game = ScheduledGame(
            game_id=row.gameId,
            season=season,
            game_date=tip.astimezone(ET).date(),
            tipoff_utc=tip,
            home=row.homeTeam_teamTricode,
            away=row.awayTeam_teamTricode,
        )
        scheduled.append(game)
        if int(row.gameStatus) == FINAL_STATUS and pd.notna(row.homeTeam_score):
            results.append(GameResult(
                **game.model_dump(),
                home_pts=int(row.homeTeam_score),
                away_pts=int(row.awayTeam_score),
                available_at=tip + result_delay,
            ))
    return scheduled, results


def parse_team_logs(df: pd.DataFrame, results: list[GameResult]) -> list[TeamBox]:
    by_id = {r.game_id: r for r in results}
    df = df.copy()
    df["GAME_ID"] = df["GAME_ID"].astype(str).str.zfill(10)
    out: list[TeamBox] = []
    for row in df.itertuples(index=False):
        r = by_id.get(row.GAME_ID)
        if r is None or row.TEAM_ABBREVIATION not in (r.home, r.away):
            continue
        is_home = r.home == row.TEAM_ABBREVIATION
        poss = float(row.FGA) + 0.44 * float(row.FTA) - float(row.OREB) + float(row.TOV)
        out.append(TeamBox(
            game_id=r.game_id,
            team=row.TEAM_ABBREVIATION,
            opponent=r.away if is_home else r.home,
            is_home=is_home,
            tipoff_utc=r.tipoff_utc,
            pts=r.home_pts if is_home else r.away_pts,
            opp_pts=r.away_pts if is_home else r.home_pts,
            possessions=poss,
            available_at=r.available_at,
        ))
    return out


def load_store(cfg: DataConfig, seasons: list[str] | None = None) -> DataStore:
    """Build a DataStore from the local cache. Raises if a season isn't cached yet
    (run `forecast-lab data refresh` first) rather than silently fetching."""
    delay = timedelta(minutes=cfg.result_delay_minutes)
    scheduled, results, box = [], [], []
    for season in seasons or cfg.seasons:
        sched_path = cfg.cache_dir / f"schedule_{season}.csv"
        logs_path = cfg.cache_dir / f"teamlogs_{season}.csv"
        if not sched_path.exists():
            raise FileNotFoundError(
                f"{sched_path} missing. Run `forecast-lab data refresh` first."
            )
        s, r = parse_schedule(pd.read_csv(sched_path, dtype={"gameId": str}), season, delay)
        scheduled += s
        results += r
        if logs_path.exists():
            box += parse_team_logs(pd.read_csv(logs_path, dtype={"GAME_ID": str}), r)
    return DataStore(results=results, schedule=scheduled, box=box, source="nba_api")
