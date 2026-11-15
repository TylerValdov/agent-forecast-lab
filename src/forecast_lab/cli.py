"""Command-line entry point: `forecast-lab <command>`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path


def _load_store(seasons: list[str] | None = None, live: bool = False, refresh: bool = False):
    """Replay/baselines use the frozen historical seasons in configs/data.yaml (so the data
    snapshot hash is stable). Live mode adds the current season, re-fetched if `refresh`."""
    from forecast_lab.data.nba_loader import DataConfig, current_season, fetch_season, load_store

    cfg = DataConfig.load()
    if live:
        if refresh:
            fetch_season(current_season(), cfg, force=True)
        seasons = cfg.seasons + [current_season()]
    return load_store(cfg, seasons)


def cmd_data_refresh(a) -> None:
    from forecast_lab.data.nba_loader import DataConfig, current_season, refresh

    cfg = DataConfig.load()
    seasons = a.seasons or cfg.seasons + [current_season()]
    refresh(cfg, seasons, force=a.force)
    store = _load_store(seasons)
    print(json.dumps({"seasons": seasons, "snapshot": store.snapshot_hash,
                      "results": len(store.results(datetime.now(UTC)))}, indent=2))


def cmd_baselines(a) -> None:
    from forecast_lab.runner.baselines import run_baselines

    store = _load_store()
    out = run_baselines(store, date.fromisoformat(a.eval_from), a.lead_minutes,
                        include_ml=not a.no_ml, n_resamples=a.resamples)
    import pandas as pd

    print(pd.read_csv(out / "scoreboard.csv").round(4).to_string(index=False))
    print(f"\nwritten to {out}")


def _llm(cfg, fake: bool):
    if fake:
        from forecast_lab.agents.fake import HeuristicFakeLLM

        print("WARNING: using the offline fake LLM (pipeline test only; not a real result)")
        return HeuristicFakeLLM()
    from forecast_lab.agents.llm import AnthropicLLM

    return AnthropicLLM(cfg.model, effort=cfg.effort)


def cmd_run(a) -> None:
    from forecast_lab.runner.config import load_config
    from forecast_lab.runner.prereg import check_preregistered
    from forecast_lab.runner.replay import run_replay

    cfg, h = load_config(a.config)
    prereg = check_preregistered(Path(a.config), h, exploratory=a.exploratory or a.fake_llm)
    if cfg.mode == "replay_raw":
        print("NOTE: replay_raw LLM results are contaminated and never primary evidence.")
    store = _load_store()
    out = run_replay(cfg, prereg, store, _llm(cfg, a.fake_llm),
                     out_root=Path("runs_fake") if a.fake_llm else Path("runs"))
    print(json.dumps(json.loads((out / "manifest.json").read_text())
                     | {"config": "...", "hint_game_ids": "..."}, indent=2))


def _now(a) -> datetime:
    if a.now:
        if not a.dry_run:
            sys.exit("--now is only allowed together with --dry-run")
        return datetime.fromisoformat(a.now).astimezone(UTC)
    return datetime.now(UTC)


def _live_dir(a) -> Path:
    return Path("runs/live_dryrun") if a.dry_run else Path("runs/live")


def cmd_live_predict(a) -> None:
    from forecast_lab.baselines.market import OddsAPIMarket
    from forecast_lab.runner.config import load_config
    from forecast_lab.runner.live import live_predict
    from forecast_lab.runner.prereg import check_preregistered

    cfg, h = load_config(a.config)
    prereg = check_preregistered(Path(a.config), h, exploratory=a.dry_run)
    store = _load_store(live=True, refresh=not a.dry_run)
    llm = None if a.baselines_only else _llm(cfg, a.fake_llm)
    print(json.dumps(live_predict(cfg, prereg, store, llm, _now(a), _live_dir(a),
                                  market=OddsAPIMarket()), indent=2))


def cmd_live_resolve(a) -> None:
    from forecast_lab.runner.config import load_config
    from forecast_lab.runner.live import live_resolve

    cfg, _ = load_config(a.config)
    store = _load_store(live=True, refresh=not a.dry_run)
    llm = None if a.baselines_only else _llm(cfg, a.fake_llm)
    print(json.dumps(live_resolve(store, llm, _now(a), _live_dir(a)), indent=2))


def cmd_report(a) -> None:
    from forecast_lab.report.build import build_report

    runs = [Path(p) for p in a.runs] if a.runs else None
    print(f"wrote {build_report(Path('runs'), Path('REPORT.md'), runs)}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="forecast-lab")
    sub = p.add_subparsers(required=True)

    s = sub.add_parser("data", help="data management").add_subparsers(required=True)
    r = s.add_parser("refresh", help="download + cache NBA data")
    r.add_argument("--seasons", nargs="*")
    r.add_argument("--force", action="store_true")
    r.set_defaults(fn=cmd_data_refresh)

    b = sub.add_parser("baselines", help="walk-forward baseline scoreboard")
    b.add_argument("--eval-from", default="2022-10-01")
    b.add_argument("--lead-minutes", type=int, default=60)
    b.add_argument("--no-ml", action="store_true")
    b.add_argument("--resamples", type=int, default=10_000)
    b.set_defaults(fn=cmd_baselines)

    e = sub.add_parser("run", help="run a pre-registered replay experiment")
    e.add_argument("config")
    e.add_argument("--exploratory", action="store_true",
                   help="allow an uncommitted config; the run is labelled exploratory")
    e.add_argument("--fake-llm", action="store_true", help="offline pipeline test")
    e.set_defaults(fn=cmd_run)

    lv = sub.add_parser("live", help="live mode jobs").add_subparsers(required=True)
    for name, fn in [("predict", cmd_live_predict), ("resolve", cmd_live_resolve)]:
        q = lv.add_parser(name)
        q.add_argument("--config", default="configs/experiments/live_v1.yaml")
        q.add_argument("--dry-run", action="store_true",
                       help="use cached data, write to runs/live_dryrun/")
        q.add_argument("--now", help="simulated clock (ISO 8601), dry-run only")
        q.add_argument("--baselines-only", action="store_true")
        q.add_argument("--fake-llm", action="store_true")
        q.set_defaults(fn=fn)

    rp = sub.add_parser("report", help="generate REPORT.md")
    rp.add_argument("runs", nargs="*", help="run dirs (default: all)")
    rp.set_defaults(fn=cmd_report)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
