"""Build a scoreboard from a long table of predictions joined with outcomes."""

from __future__ import annotations

import numpy as np
import pandas as pd

from forecast_lab.eval.metrics import accuracy, brier, ece, log_loss
from forecast_lab.eval.stats import holm, paired_bootstrap


def scoreboard(df: pd.DataFrame, reference: str, n_resamples: int = 10_000, seed: int = 0,
               min_n: int = 0) -> pd.DataFrame:
    """`df` columns: game_id, model, prob, y. Only games where every model has a prediction
    are used (paired design). Paired differences are vs `reference`."""
    wide = df.pivot_table(index="game_id", columns="model", values="prob", aggfunc="first")
    y = df.groupby("game_id")["y"].first().reindex(wide.index)
    wide = wide.dropna()
    y = y.loc[wide.index].to_numpy()
    rows = []
    ref_b = brier(wide[reference].to_numpy(), y) if reference in wide else None
    for model in wide.columns:
        p = wide[model].to_numpy()
        b = brier(p, y)
        row = {
            "model": model, "n": len(p), "brier": b.mean(), "log_loss": log_loss(p, y).mean(),
            "accuracy": accuracy(p, y), "ece": ece(p, y),
        }
        if ref_b is not None and model != reference:
            pr = paired_bootstrap(b, ref_b, n_resamples=n_resamples, seed=seed)
            row |= {"d_brier_vs_ref": pr.mean_diff, "ci_low": pr.ci_low,
                    "ci_high": pr.ci_high, "p": pr.p_value, "d_z": pr.effect_size}
        rows.append(row)
    out = pd.DataFrame(rows).sort_values("brier").reset_index(drop=True)
    if "p" in out:
        mask = out["p"].notna()
        out.loc[mask, "p_holm"] = holm(out.loc[mask, "p"].tolist())
    out["status"] = np.where(out["n"] >= min_n, "ok", "insufficient data")
    return out
