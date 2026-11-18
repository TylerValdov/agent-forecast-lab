"""Generate REPORT.md (and figures) from run artifacts. Nothing here calls an API.

Scoring policy for agent failures ("intent to treat"): a prediction whose agent failed
(no valid submit_forecast) is scored at the recorded sentinel 0.5 and counted in the
failure table. Results excluding failures are reported alongside.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from forecast_lab.eval.metrics import brier, brier_decomposition, ece
from forecast_lab.eval.scoreboard import scoreboard
from forecast_lab.eval.stats import benjamini_hochberg, holm, paired_bootstrap
from forecast_lab.ledger.jsonl import Ledger
from forecast_lab.report.plots import forest_plot, reliability_diagram
from forecast_lab.runner.baselines import load_baseline_predictions

SENTINEL = 0.5
BASELINES_SHOWN = ["elo", "logistic", "gbm", "home_rate", "always_0.5"]


def _fmt(x, nd=4):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "–"
    if isinstance(x, float | np.floating):
        if abs(x) >= 100:
            return f"{x:,.0f}"
        if 0 < abs(x) < 0.01:
            return f"{x:.2e}"
        return f"{x:.{nd}f}"
    return str(x)


def _table(df: pd.DataFrame, nd: int = 4) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(_fmt(row[c], nd) for c in cols) + " |")
    return "\n".join(lines)


def load_run(run_dir: Path) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    preds = pd.DataFrame([r for r in Ledger(run_dir / "predictions.jsonl").verify()
                          if r.get("kind") == "agent"])
    outs = pd.DataFrame(Ledger(run_dir / "outcomes.jsonl").verify())
    return manifest, preds, outs


def agent_long(preds: pd.DataFrame, outs: pd.DataFrame) -> pd.DataFrame:
    """One row per (game, variant): prob averaged over repeats, failures -> sentinel."""
    if preds.empty or outs.empty:
        return pd.DataFrame(columns=["game_id", "model", "prob", "y", "failed"])
    df = preds.merge(outs[["game_id", "home_win"]], on="game_id", how="inner")
    df["failed"] = df["prob"].isna()
    df["prob_itt"] = df["prob"].fillna(SENTINEL)
    g = df.groupby(["game_id", "variant"]).agg(
        prob=("prob_itt", "mean"), y=("home_win", "first"), failed=("failed", "max")
    ).reset_index().rename(columns={"variant": "model"})
    g["model"] = "agent_" + g["model"]
    g["y"] = g["y"].astype(int)
    return g


def section_run(run_dir: Path, runs_root: Path, fig_dir: Path) -> str:
    manifest, preds, outs = load_run(run_dir)
    cfg = manifest["config"]
    ev = cfg["evaluation"]
    mode = manifest["mode"]
    name = f"{manifest['experiment']} / {manifest['run_id']}"
    primary = mode != "replay_raw"
    lines = [f"## Experiment `{name}`", ""]
    lines += [
        f"- **Mode:** `{mode}`" + ("" if primary else " (NOT primary evidence: raw replay "
                                    "of an LLM is contaminated by memorization)"),
        "- **Status:** "
        + ("EXPLORATORY (config not committed at run time)"
           if manifest["prereg"]["exploratory"] else
           f"{manifest['status']}, config sha256 `{manifest['prereg']['config_sha256'][:16]}`, "
           f"commit `{(manifest['prereg']['git_commit'] or '')[:10]}`"),
        f"- **Model:** `{manifest['model']}` (effort `{manifest['effort']}`, backend "
        f"`{manifest.get('backend', 'api')}`), prompts "
        f"`{manifest['prompt_version']}` (bundle `{manifest['prompt_bundle_hash']}`), "
        f"repeats {manifest['repeats']}",
        f"- **Data snapshot:** `{manifest['data_snapshot']}`; games selected "
        f"{manifest['n_games_selected']}; predictions {manifest.get('n_predictions', '?')}; "
        f"stop: {manifest.get('stopped_reason', 'unknown')}",
        f"- **Spend:** ${manifest.get('usd_spent', 0):.4f}, "
        f"{manifest.get('tokens_spent', 0):,} tokens",
        "",
    ]
    if preds.empty:
        return "\n".join(lines + ["_No agent predictions recorded._", ""])

    long = agent_long(preds, outs)
    base = load_baseline_predictions(runs_root, manifest["data_snapshot"], cfg["lead_minutes"])
    if base is not None:
        base = base[base["game_id"].isin(long["game_id"]) & base["prob"].notna()]
        base = base[base["model"].isin(BASELINES_SHOWN)][["game_id", "model", "prob", "y"]]
        combined = pd.concat([long[["game_id", "model", "prob", "y"]], base])
    else:
        combined = long[["game_id", "model", "prob", "y"]]
        lines.append("_Baseline predictions for this data snapshot not found; run "
                     "`forecast-lab baselines` first._\n")

    # Scoreboard on games all of A/B/C + baselines share (D is on a subset, so excluded).
    main = combined[combined["model"] != "agent_D"]
    ref = ev["reference_baseline"] if ev["reference_baseline"] in set(main["model"]) \
        else "agent_A"
    board = scoreboard(main, reference=ref, n_resamples=ev["bootstrap_resamples"],
                       seed=ev["bootstrap_seed"], min_n=ev["min_n"])
    lines += [f"### Scoreboard ({mode}; paired on common games; Δ vs `{ref}`; "
              "negative Δ = better than reference)", "",
              _table(board[[c for c in ["model", "n", "brier", "log_loss", "accuracy", "ece",
                                         "d_brier_vs_ref", "ci_low", "ci_high", "p_holm",
                                         "status"] if c in board]]), ""]
    fig = reliability_diagram(main, fig_dir / f"{manifest['run_id']}_calibration.png",
                              f"{manifest['experiment']} ({mode})")
    lines += [f"![calibration]({fig.as_posix()})", ""]

    # Pre-registered comparisons with multiple-comparison correction.
    wide = combined.pivot_table(index="game_id", columns="model", values="prob")
    y = combined.groupby("game_id")["y"].first()
    comp_rows = []
    for c in ev["comparisons"]:
        a = c["a"] if c["a"] in wide else f"agent_{c['a']}"
        b = c["b"] if c["b"] in wide else f"agent_{c['b']}"
        if a not in wide or b not in wide:
            continue
        pair = wide[[a, b]].dropna()
        yy = y.loc[pair.index].to_numpy()
        r = paired_bootstrap(brier(pair[a].to_numpy(), yy), brier(pair[b].to_numpy(), yy),
                             ev["bootstrap_resamples"], ev["bootstrap_seed"])
        comp_rows.append({"rq": c["rq"], "comparison": f"{a} vs {b}", "n": r.n,
                          "d_brier": r.mean_diff, "ci_low": r.ci_low, "ci_high": r.ci_high,
                          "d_z": r.effect_size, "p": r.p_value})
    if comp_rows:
        comp = pd.DataFrame(comp_rows)
        comp["p_holm"] = holm(comp["p"].tolist())
        comp["p_bh"] = benjamini_hochberg(comp["p"].tolist())
        comp["status"] = np.where(comp["n"] >= ev["min_n"], "ok", "insufficient data")
        lines += [f"### Pre-registered comparisons ({len(comp)} tested; Holm and BH "
                  "corrected; 10k-resample paired bootstrap unless configured otherwise)", "",
                  _table(comp), ""]
        fp = forest_plot(comp.rename(columns={"d_brier": "mean_diff", "comparison": "label"}),
                         fig_dir / f"{manifest['run_id']}_comparisons.png",
                         f"Pre-registered comparisons ({mode})")
        lines += [f"![comparisons]({fp.as_posix()})", ""]

    lines += _section_critique(preds, outs) + _section_hints(preds, outs, ev["min_n"])
    lines += _section_memory(long) + _section_cost(preds, outs) + _section_failures(preds)
    return "\n".join(lines)


def _section_critique(preds, outs) -> list[str]:
    c = preds[(preds["variant"] == "C") & preds["prob"].notna() & preds["draft_prob"].notna()]
    if c.empty:
        return []
    c = c.merge(outs[["game_id", "home_win"]], on="game_id")
    y = c["home_win"].astype(int).to_numpy()
    d, f = c["draft_prob"].to_numpy(), c["prob"].to_numpy()
    r = paired_bootstrap(brier(f, y), brier(d, y))
    return ["### RQ2: self-critique, draft vs final (variant C)", "",
            f"- n = {len(c)}; Brier draft {brier(d, y).mean():.4f} → final "
            f"{brier(f, y).mean():.4f}; Δ = {r.mean_diff:+.4f} "
            f"[{r.ci_low:+.4f}, {r.ci_high:+.4f}]",
            f"- Mean confidence |p−0.5|: draft {np.abs(d - .5).mean():.3f} → final "
            f"{np.abs(f - .5).mean():.3f}; revised in {np.mean(np.abs(f - d) > 1e-9):.0%} "
            "of games", ""]


def _section_hints(preds, outs, min_n) -> list[str]:
    d = preds[(preds["variant"] == "D") & preds["prob"].notna()]
    a = preds[(preds["variant"] == "A") & preds["prob"].notna()]
    if d.empty:
        return []
    m = d.merge(a, on=["game_id", "repeat"], suffixes=("_d", "_a")).merge(
        outs[["game_id", "home_win"]], on="game_id")
    if m.empty:
        return []
    y = m["home_win"].astype(int).to_numpy()
    pd_, pa = m["prob_d"].to_numpy(), m["prob_a"].to_numpy()
    direction = np.array([h["direction"] for h in m["hint_d"]])
    shift = pd_ - pa
    flips = np.mean((pd_ > 0.5) != (pa > 0.5))
    r = paired_bootstrap(brier(pd_, y), brier(pa, y))
    status = "" if len(m) >= min_n else " — **insufficient data**"
    by_t = pd.DataFrame({"template": [h["template"] for h in m["hint_d"]],
                         "toward_hint": shift * direction}).groupby("template")["toward_hint"]
    tmpl = ", ".join(f"{k}: {v:+.3f} (n={n})" for (k, v), n in
                     zip(by_t.mean().items(), by_t.size(), strict=True))
    return ["### RQ3: robustness to a false hint (variant D vs A, paired)" + status, "",
            f"- n = {len(m)} hinted games; mean |Δp| = {np.abs(shift).mean():.3f}; mean shift "
            f"toward the hint = {(shift * direction).mean():+.3f}; flip rate (crossing 0.5) "
            f"= {flips:.1%}",
            f"- Brier D − A = {r.mean_diff:+.4f} [{r.ci_low:+.4f}, {r.ci_high:+.4f}]",
            f"- Shift toward hint by template: {tmpl}", ""]


def _section_memory(long) -> list[str]:
    b = long[long["model"] == "agent_B"].sort_values("game_id")
    a = long[long["model"] == "agent_A"].set_index("game_id")
    if b.empty or a.empty:
        return []
    out = ["### RQ1: memory (variant B) calibration", "",
           "| half | n | ECE A | ECE B | Brier A | Brier B |", "|---|---|---|---|---|---|"]
    mid = len(b) // 2  # game_ids sort chronologically within a season
    halves = [b.iloc[:mid], b.iloc[mid:]]
    for name, h in zip(["first half", "second half"], halves, strict=True):
        aa = a.loc[h["game_id"]]
        out.append(f"| {name} | {len(h)} | {ece(aa['prob'], aa['y']):.4f} | "
                   f"{ece(h['prob'], h['y']):.4f} | {brier(aa['prob'], aa['y']).mean():.4f} | "
                   f"{brier(h['prob'], h['y']).mean():.4f} |")
    dec = brier_decomposition(b["prob"].to_numpy(), b["y"].to_numpy())
    out += ["", f"Brier decomposition (B): reliability {dec.reliability:.4f}, resolution "
                f"{dec.resolution:.4f}, uncertainty {dec.uncertainty:.4f}", ""]
    return out


def _section_cost(preds, outs) -> list[str]:
    m = preds.merge(outs[["game_id", "home_win"]], on="game_id")
    m["tok"] = m["tokens"].apply(lambda t: sum(t.values()))
    m["b"] = (m["prob"].fillna(SENTINEL) - m["home_win"].astype(int)) ** 2
    g = m.groupby("variant").agg(n=("game_id", "size"), usd_per_pred=("cost_usd", "mean"),
                                 tokens_per_pred=("tok", "mean"),
                                 latency_s=("latency_s", "mean"), steps=("steps", "mean"),
                                 brier=("b", "mean")).reset_index()
    g["skill_per_usd"] = (0.25 - g["brier"]) / g["usd_per_pred"].replace(0, np.nan)
    return ["### Cost and latency per prediction", "",
            "`skill_per_usd` = Brier improvement over the always-0.5 forecast, per dollar "
            "per prediction.", "", _table(g, 4), ""]


def _section_failures(preds) -> list[str]:
    g = preds.groupby("variant").agg(
        n=("game_id", "size"),
        failed=("prob", lambda s: s.isna().mean()),
        step_limit=("failure", lambda s: (s == "step_limit").mean()),
        invalid_outputs=("invalid_submissions", "sum"),
        tool_errors=("tool_errors", "sum"),
    ).reset_index()
    out = ["### Failure accounting", "", _table(g, 3), ""]
    fails = preds[preds["prob"].isna()].head(3)
    if not fails.empty:
        out += ["#### Inspected failure trajectories", ""]
        for _, r in fails.iterrows():
            steps = [f"{t.get('name')}({json.dumps(t.get('input'))})"
                     for t in r["trajectory"] if t.get("type") == "tool_result"]
            out.append(f"- `{r['game_id']}` variant {r['variant']}: failure "
                       f"`{r['failure']}` after {r['steps']} steps; calls: "
                       f"{'; '.join(steps) or 'none'}")
        out.append("")
    return out


def section_baselines(runs_root: Path, fig_dir: Path) -> str:
    dirs = sorted((runs_root / "baselines").glob("*/manifest.json"))
    if not dirs:
        return "## Baselines\n\n_Not run yet: `forecast-lab baselines`._\n"
    d = dirs[-1].parent
    m = json.loads((d / "manifest.json").read_text())
    board = pd.read_csv(d / "scoreboard.csv")
    fig_dir.mkdir(parents=True, exist_ok=True)
    fig = fig_dir / "baselines_calibration.png"
    fig.write_bytes((d / "calibration.png").read_bytes())
    cols = [c for c in ["model", "n", "brier", "log_loss", "accuracy", "ece",
                        "d_brier_vs_ref", "ci_low", "ci_high", "p_holm"] if c in board]
    return "\n".join([
        "## Baselines (mode `replay_raw`, walk-forward)", "",
        f"Data snapshot `{m['data_snapshot']}`, forecasts made {m['lead_minutes']} min before "
        f"tip-off, evaluated on {m['n_games_eval']} regular-season games from "
        f"{m['eval_from']}. Δ vs Elo, 95% paired bootstrap CI.", "",
        _table(board[cols]), "", f"![baseline calibration]({fig.as_posix()})",
        "", "Market baseline: **missing** for historical games (no free historical odds "
        "source; not approximated).", ""])


def section_live(live_dir: Path) -> str:
    p = live_dir / "predictions.jsonl"
    if not p.exists():
        return "## Live mode\n\n_No live predictions yet._\n"
    recs = Ledger(p).verify()
    outs = pd.DataFrame(Ledger(live_dir / "outcomes.jsonl").verify()) \
        if (live_dir / "outcomes.jsonl").exists() else pd.DataFrame()
    df = pd.DataFrame([r for r in recs if r.get("kind") in ("agent", "baseline")])
    df["model"] = np.where(df["kind"] == "agent", "agent_" + df.get("variant", "").astype(str),
                           df["model"])
    late = (pd.to_datetime(df["timestamp"]) >= pd.to_datetime(df["tipoff_utc"])).sum()
    lines = ["## Live mode (mode `live`, primary evidence)", "",
             f"{df['game_id'].nunique()} games forecast; {len(df)} predictions; "
             f"{late} recorded at/after tip-off (should be 0). Ledger chain verified.", ""]
    if outs.empty:
        return "\n".join(lines + ["_No resolved games yet._", ""])
    m = df.merge(outs[["game_id", "home_win"]], on="game_id")
    m["y"] = m["home_win"].astype(int)
    m["prob"] = m["prob"].fillna(SENTINEL)
    board = scoreboard(m[["game_id", "model", "prob", "y"]], reference="elo", n_resamples=10_000,
                       min_n=100)
    return "\n".join(lines + [_table(board), ""])


LIMITATIONS = """## Limitations

- **Contamination.** LLMs may have memorized historical NBA results. Only `live` results are
  clean. `replay_anonymized` masks team names, player names and dates in prompts and tool
  outputs, which reduces but does not eliminate contamination (a model could still infer a
  team from its statistical profile). `replay_raw` LLM numbers are never primary evidence.
- **Sample size.** Single-game outcomes are noisy; differences in Brier of ~0.002 need
  thousands of games to resolve. Comparisons below the configured minimum sample are
  marked "insufficient data".
- **Market data.** No free historical odds source was available, so the market baseline is
  missing in replay modes and is not approximated. In live mode it requires an
  `ODDS_API_KEY`.
- **Injury data.** Historical, timestamped injury reports were not available in a reliable
  machine-readable form, so the injury tool is disabled in replay modes.
- **Result timing.** Historical data has tipoff times but not final-whistle times, so a
  result is treated as knowable 210 minutes after tipoff (conservative).
- **API model drift.** Hosted models can change behind a fixed model ID. Every record logs
  the model ID and date; results are conditional on that snapshot.
- **No sampling control.** Current Claude models reject temperature settings, so run-to-run
  variance is estimated with repeated runs (`repeats`) instead of a fixed temperature.
"""


def build_report(runs_root: Path = Path("runs"), out: Path = Path("REPORT.md"),
                 run_dirs: list[Path] | None = None) -> Path:
    fig_dir = out.parent / "docs" / "figures"
    if run_dirs is None:
        run_dirs = sorted(p.parent for p in runs_root.glob("*/*/manifest.json")
                          if p.parent.parent.name not in ("baselines", "live"))
    parts = [
        "# Results report", "",
        f"_Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `forecast-lab report`. "
        "Every number is labelled with the mode that produced it._", "",
        section_baselines(runs_root, fig_dir),
        *(section_run(d, runs_root, fig_dir) for d in run_dirs),
        section_live(runs_root / "live"),
        LIMITATIONS,
    ]
    out.write_text("\n".join(parts), encoding="utf-8")
    return out
