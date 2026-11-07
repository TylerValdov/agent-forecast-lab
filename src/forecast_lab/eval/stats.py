"""Paired comparisons: bootstrap CIs and multiple-comparison corrections."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class PairedResult:
    n: int
    mean_diff: float  # mean(loss_a - loss_b); negative => A is better (lower loss)
    ci_low: float
    ci_high: float
    p_value: float  # two-sided bootstrap p-value for H0: mean_diff == 0
    effect_size: float  # mean_diff / sd(diff)  (paired Cohen's d_z)


def paired_bootstrap(loss_a: np.ndarray, loss_b: np.ndarray, n_resamples: int = 10_000,
                     seed: int = 0, alpha: float = 0.05) -> PairedResult:
    """Percentile bootstrap over games of the paired per-game loss difference."""
    d = np.asarray(loss_a, float) - np.asarray(loss_b, float)
    n = len(d)
    if n == 0:
        nan = float("nan")
        return PairedResult(0, nan, nan, nan, nan, nan)
    rng = np.random.default_rng(seed)
    chunk = max(1, 2_000_000 // n)  # bound memory for large n
    means = np.concatenate([
        d[rng.integers(0, n, size=(min(chunk, n_resamples - i), n))].mean(axis=1)
        for i in range(0, n_resamples, chunk)
    ])
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    # p-value: shift the bootstrap distribution to be centred on 0 (null), two-sided.
    centred = means - d.mean()
    p = float(np.mean(np.abs(centred) >= abs(d.mean())))
    sd = d.std(ddof=1) if n > 1 else float("nan")
    return PairedResult(n, float(d.mean()), float(lo), float(hi), max(p, 1 / n_resamples),
                        float(d.mean() / sd) if sd and sd > 0 else float("nan"))


def holm(p_values: list[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values (family-wise error control)."""
    m = len(p_values)
    order = np.argsort(p_values)
    adj = np.empty(m)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p_values[i]))
        adj[i] = running
    return adj.tolist()


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    """BH adjusted p-values (false-discovery-rate control)."""
    m = len(p_values)
    order = np.argsort(p_values)[::-1]
    adj = np.empty(m)
    running = 1.0
    for rank_from_top, i in enumerate(order):
        rank = m - rank_from_top
        running = min(running, p_values[i] * m / rank)
        adj[i] = running
    return adj.tolist()
