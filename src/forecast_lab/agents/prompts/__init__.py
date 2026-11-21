"""Versioned prompts. Changing a prompt means adding a new `_vN.md` file and bumping the
version in the experiment config; old versions stay so past runs remain reproducible.

`bundle_hash` covers the core agent prompts (system, critique, note_writer). Prompts used
only by specific variants or probes (guardrail, recall_probe) are hashed separately with
`prompt_hash` and recorded in the manifest when they are used, so adding one never changes
the bundle hash of runs that don't use it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_DIR = Path(__file__).parent
CORE = ("critique", "note_writer", "system")


def load(name: str, version: str) -> str:
    return (_DIR / f"{name}_{version}.md").read_text(encoding="utf-8").strip()


def bundle_hash(version: str) -> str:
    h = hashlib.sha256()
    for p in sorted(_DIR / f"{n}_{version}.md" for n in CORE):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def prompt_hash(name: str, version: str) -> str:
    return hashlib.sha256((_DIR / f"{name}_{version}.md").read_bytes()).hexdigest()[:16]
