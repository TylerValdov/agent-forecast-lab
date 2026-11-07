"""One command -> the reproducible baseline scoreboard (mode: replay_raw)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from forecast_lab.baselines.base import walk_forward
from forecast_lab.baselines.elo import Elo, EloParams
from forecast_lab.baselines.ml import WalkForwardML
from forecast_lab.baselines.naive import AlwaysHalf, HomeRate
from forecast_lab.data.store import DataStore
from forecast_lab.eval.scoreboard import scoreboard
from forecast_lab.report.plots import reliability_diagram

FAR = datetime(2100, 1, 1, tzinfo=UTC)


def default_forecasters(lead: timedelta, include_ml: bool = True) -> list:
    fs = [AlwaysHalf(), HomeRate(), Elo()]
    if include_ml:
        fs += [WalkForwardML("logistic", lead=lead), WalkForwardML("gbm", lead=lead)]
    return fs


def baseline_dir(out_root: Path, snapshot: str, lead_minutes: int) -> Path:
    return out_root / "baselines" / f"{snapshot}_lead{lead_minutes}"


def run_baselines(store: DataStore, eval_from: date, lead_minutes: int = 60,
                  out_root: Path = Path("runs"), include_ml: bool = True,
                  n_resamples: int = 10_000) -> Path:
    lead = timedelta(minutes=lead_minutes)
    out = baseline_dir(out_root, store.snapshot_hash, lead_minutes)
    out.mkdir(parents=True, exist_ok=True)
    games = [g for g in store.scheduled_between(datetime(1990, 1, 1, tzinfo=UTC), FAR)
             if store.reveal(g.game_id, FAR) is not None]
    fs = default_forecasters(lead, include_ml)
    preds = walk_forward(store, games, fs, lead=lead)

    rows = []
    for p in preds:
        r = store.reveal(p.game_id, FAR)
        rows.append({"game_id": p.game_id, "model": p.model, "as_of": p.as_of.isoformat(),
                     "tipoff_utc": r.tipoff_utc.isoformat(), "season": r.season,
                     "prob": p.prob, "y": int(r.home_win)})
    df = pd.DataFrame(rows)
    df.to_csv(out / "predictions.csv", index=False)

    start = datetime.combine(eval_from, time(), tzinfo=UTC).isoformat()
    ev = df[(df["as_of"] >= start) & df["prob"].notna()]
    board = scoreboard(ev, reference="elo", n_resamples=n_resamples)
    board.to_csv(out / "scoreboard.csv", index=False)
    reliability_diagram(ev, out / "calibration.png",
                        f"Baselines, replay_raw, n={ev['game_id'].nunique()} games")
    ml_fits = {f.name: [{"fit_as_of": r.fit_as_of.isoformat(), "n_train": r.n_train,
                         "max_label_available_at": r.max_label_available_at.isoformat(),
                         "params": r.params} for r in f.fit_log]
               for f in fs if isinstance(f, WalkForwardML)}
    manifest = {
        "mode": "replay_raw", "data_snapshot": store.snapshot_hash, "data_source": store.source,
        "lead_minutes": lead_minutes, "eval_from": eval_from.isoformat(),
        "n_games_eval": int(ev["game_id"].nunique()),
        "elo_params": EloParams().__dict__, "created_at": datetime.now(UTC).isoformat(),
        "ml_fits": ml_fits,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return out


def load_baseline_predictions(out_root: Path, snapshot: str, lead_minutes: int
                              ) -> pd.DataFrame | None:
    p = baseline_dir(out_root, snapshot, lead_minutes) / "predictions.csv"
    return pd.read_csv(p, dtype={"game_id": str}) if p.exists() else None
