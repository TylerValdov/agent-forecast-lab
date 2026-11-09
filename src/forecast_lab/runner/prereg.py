"""Pre-registration gate: a run only starts if its config is committed and unmodified.

A run with `--exploratory` may skip the gate, but is labelled exploratory everywhere.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class PreregistrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreregRecord:
    config_path: str
    config_sha256: str
    git_commit: str | None
    committed: bool
    exploratory: bool


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def check_preregistered(config_path: Path, config_sha256: str, exploratory: bool = False,
                        repo: Path | None = None) -> PreregRecord:
    config_path = Path(config_path).resolve()
    repo = repo or config_path.parent
    rel = str(config_path)
    tracked = _git("ls-files", "--error-unmatch", rel, cwd=repo).returncode == 0
    dirty = bool(_git("status", "--porcelain", "--", rel, cwd=repo).stdout.strip())
    head = _git("rev-parse", "HEAD", cwd=repo)
    commit = head.stdout.strip() if head.returncode == 0 else None
    committed = tracked and not dirty and commit is not None
    if not committed and not exploratory:
        raise PreregistrationError(
            f"{config_path.name} is not committed (tracked={tracked}, modified={dirty}). "
            "Commit the config before running, or pass --exploratory to label the run "
            "as exploratory."
        )
    return PreregRecord(str(config_path.name), config_sha256, commit, committed, exploratory)
