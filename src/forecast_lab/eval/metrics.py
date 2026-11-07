"""Proper scoring rules and calibration.

ECE binning: 10 equal-width bins on [0, 1]; ECE = sum_b (n_b / N) * |mean(p_b) - mean(y_b)|.
Brier decomposition (Murphy 1973) uses the same bins:
    Brier ~= reliability - resolution + uncertainty
(exact only when forecasts are constant within bins; the residual is reported).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

N_BINS = 10
EPS = 1e-12


def brier(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Per-game Brier score."""
    return (np.asarray(p, float) - np.asarray(y, float)) ** 2


def log_loss(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Per-game log loss (natural log)."""
    p = np.clip(np.asarray(p, float), EPS, 1 - EPS)
    y = np.asarray(y, float)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def accuracy(p: np.ndarray, y: np.ndarray) -> float:
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p > 0.5) == (y == 1)))


@dataclass(frozen=True)
class CalibrationBins:
    edges: np.ndarray
    counts: np.ndarray
    mean_pred: np.ndarray  # NaN where empty
    frac_pos: np.ndarray


def calibration_bins(p: np.ndarray, y: np.ndarray, n_bins: int = N_BINS) -> CalibrationBins:
    p, y = np.asarray(p, float), np.asarray(y, float)
    edges = np.linspace(0, 1, n_bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1], right=False), 0, n_bins - 1)
    counts = np.bincount(idx, minlength=n_bins)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_pred = np.bincount(idx, weights=p, minlength=n_bins) / counts
        frac_pos = np.bincount(idx, weights=y, minlength=n_bins) / counts
    return CalibrationBins(edges, counts, mean_pred, frac_pos)


def ece(p: np.ndarray, y: np.ndarray, n_bins: int = N_BINS) -> float:
    b = calibration_bins(p, y, n_bins)
    m = b.counts > 0
    return float(np.sum(b.counts[m] / b.counts.sum() * np.abs(b.mean_pred[m] - b.frac_pos[m])))


@dataclass(frozen=True)
class BrierDecomposition:
    reliability: float
    resolution: float
    uncertainty: float
    residual: float


def brier_decomposition(p: np.ndarray, y: np.ndarray, n_bins: int = N_BINS
                        ) -> BrierDecomposition:
    b = calibration_bins(p, y, n_bins)
    m = b.counts > 0
    w = b.counts[m] / b.counts.sum()
    base = float(np.mean(y))
    rel = float(np.sum(w * (b.mean_pred[m] - b.frac_pos[m]) ** 2))
    res = float(np.sum(w * (b.frac_pos[m] - base) ** 2))
    unc = base * (1 - base)
    total = float(np.mean(brier(p, y)))
    return BrierDecomposition(rel, res, unc, total - (rel - res + unc))
