"""Figures: reliability diagrams and paired-difference forest plots."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from forecast_lab.eval.metrics import calibration_bins  # noqa: E402


def reliability_diagram(df: pd.DataFrame, path: Path, title: str) -> Path:
    """`df` columns: model, prob, y. One line per model, 10 equal-width bins."""
    fig, ax = plt.subplots(figsize=(5.5, 5.5))
    ax.plot([0, 1], [0, 1], ls="--", color="grey", lw=1, label="perfect calibration")
    for model, g in df.groupby("model"):
        b = calibration_bins(g["prob"].to_numpy(), g["y"].to_numpy())
        m = b.counts >= 10  # hide bins too small to read
        ax.plot(b.mean_pred[m], b.frac_pos[m], marker="o", ms=4, lw=1.5, label=str(model))
    ax.set(xlim=(0, 1), ylim=(0, 1), xlabel="forecast P(home win)",
           ylabel="observed home-win frequency", title=title)
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def forest_plot(rows: pd.DataFrame, path: Path, title: str) -> Path:
    """`rows` columns: label, mean_diff, ci_low, ci_high. Negative = first item better."""
    rows = rows.iloc[::-1].reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(6.5, 0.5 * len(rows) + 1.2))
    y = np.arange(len(rows))
    ax.errorbar(rows["mean_diff"], y,
                xerr=[rows["mean_diff"] - rows["ci_low"], rows["ci_high"] - rows["mean_diff"]],
                fmt="o", color="black", capsize=3)
    ax.axvline(0, color="grey", ls="--", lw=1)
    ax.set_yticks(y, rows["label"])
    ax.set(xlabel="paired Brier difference (A - B), 95% bootstrap CI", title=title)
    ax.grid(alpha=0.3, axis="x")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path
