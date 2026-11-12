"""Agent memory (variant B), time-gated like everything else.

* A note is written only after the game's outcome is revealed; its `created_at` is the
  reveal clock (simulated in replay, real in live).
* When predicting a game, only notes with `created_at < as_of` are loaded, capped to the
  most recent `k`. Exactly what was loaded is logged with the prediction.
* Storage is an append-only JSONL file, so memory state is auditable and replayable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from forecast_lab.data.store import check_as_of


@dataclass(frozen=True)
class Note:
    note_id: str
    created_at: datetime
    game_id: str
    text: str

    def to_json(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


class MemoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._notes: list[Note] = []
        if path is not None and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    d = json.loads(line)
                    d["created_at"] = datetime.fromisoformat(d["created_at"])
                    self._notes.append(Note(**d))

    def add(self, note: Note) -> None:
        check_as_of(note.created_at)
        self._notes.append(note)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(note.to_json()) + "\n")

    def load(self, as_of: datetime, k: int) -> list[Note]:
        as_of = check_as_of(as_of)
        visible = sorted((n for n in self._notes if n.created_at < as_of),
                         key=lambda n: n.created_at)
        return visible[-k:] if k > 0 else []

    def __len__(self) -> int:
        return len(self._notes)


def render_memory(notes: list[Note]) -> str:
    if not notes:
        return "Your forecasting journal is empty so far."
    lines = "\n".join(f"- {n.text}" for n in notes)
    return f"Notes from your forecasting journal (most recent last):\n{lines}"
