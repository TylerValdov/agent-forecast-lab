"""Package run artifacts for a GitHub release: the full ledgers (every prediction with its
trajectory) are too large to commit, but readers should be able to re-check every number.

The zip contains each run directory as-is plus `SHA256SUMS`, and the ledgers can be
re-verified after download with `forecast_lab.ledger.jsonl.Ledger(path).verify()`.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from forecast_lab.ledger.jsonl import Ledger


def bundle_runs(run_dirs: list[Path], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    sums: list[str] = []
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for d in run_dirs:
            d = Path(d)
            for f in sorted(d.glob("*.jsonl")):
                if f.stat().st_size and f.name in ("predictions.jsonl", "outcomes.jsonl"):
                    Ledger(f).verify()  # never ship a broken chain
            for f in sorted(p for p in d.iterdir() if p.is_file()):
                arc = f"{d.parent.name}/{d.name}/{f.name}"
                data = f.read_bytes()
                z.writestr(arc, data)
                sums.append(f"{hashlib.sha256(data).hexdigest()}  {arc}")
        z.writestr("SHA256SUMS", "\n".join(sums) + "\n")
    return out
