"""Append-only, hash-chained JSONL ledger.

Each record gets `seq`, `prev_hash` and `record_hash` (sha256 of the canonical record
including prev_hash). Editing or deleting any past line breaks the chain, which
`verify()` detects. The file is only ever opened in append mode.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


class LedgerCorrupted(RuntimeError):
    pass


def _canonical(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str).encode()


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._last_hash, self._seq = GENESIS, 0
        if self.path.exists():
            records = self.verify()
            if records:
                self._last_hash, self._seq = records[-1]["record_hash"], records[-1]["seq"]

    def append(self, record: dict[str, Any]) -> dict:
        if any(k in record for k in ("seq", "prev_hash", "record_hash")):
            raise ValueError("reserved ledger fields in record")
        # Normalize through JSON first so the hashed form equals what verify() re-reads.
        body = json.loads(json.dumps(dict(record, seq=self._seq + 1,
                                          prev_hash=self._last_hash), default=str))
        body["record_hash"] = hashlib.sha256(_canonical(body)).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(body, default=str) + "\n")
            f.flush()
        self._last_hash, self._seq = body["record_hash"], body["seq"]
        return body

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def verify(self) -> list[dict]:
        prev = GENESIS
        records = self.records()
        for i, rec in enumerate(records, start=1):
            claimed = rec.get("record_hash")
            body = {k: v for k, v in rec.items() if k != "record_hash"}
            if rec.get("seq") != i or rec.get("prev_hash") != prev:
                raise LedgerCorrupted(f"{self.path}: chain broken at line {i}")
            if hashlib.sha256(_canonical(body)).hexdigest() != claimed:
                raise LedgerCorrupted(f"{self.path}: record {i} was modified")
            prev = claimed
        return records
