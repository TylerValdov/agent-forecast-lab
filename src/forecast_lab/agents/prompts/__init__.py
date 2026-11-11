"""Versioned prompts. Changing a prompt means adding a new `_vN.md` file and bumping the
version in the experiment config; old versions stay so past runs remain reproducible."""

from __future__ import annotations

import hashlib
from pathlib import Path

_DIR = Path(__file__).parent


def load(name: str, version: str) -> str:
    return (_DIR / f"{name}_{version}.md").read_text(encoding="utf-8").strip()


def bundle_hash(version: str) -> str:
    h = hashlib.sha256()
    for p in sorted(_DIR.glob(f"*_{version}.md")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]
