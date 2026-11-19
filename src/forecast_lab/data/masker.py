"""Deterministic anonymization for `replay_anonymized` mode.

Team identifiers, team names, player names and calendar dates are replaced with opaque
tokens. The mapping is keyed by a secret salt (the run seed) so it is stable within a run
(memory notes stay consistent) but not guessable across runs.

This REDUCES contamination; it cannot eliminate it (a model may still recognise a team from
its statistical profile). The report must say so.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import date, datetime

# Tricode -> (city, nickname). Used to scrub any free text that might mention a team.
NBA_TEAMS: dict[str, tuple[str, str]] = {
    "ATL": ("Atlanta", "Hawks"), "BOS": ("Boston", "Celtics"), "BKN": ("Brooklyn", "Nets"),
    "CHA": ("Charlotte", "Hornets"), "CHI": ("Chicago", "Bulls"),
    "CLE": ("Cleveland", "Cavaliers"), "DAL": ("Dallas", "Mavericks"),
    "DEN": ("Denver", "Nuggets"), "DET": ("Detroit", "Pistons"),
    "GSW": ("Golden State", "Warriors"), "HOU": ("Houston", "Rockets"),
    "IND": ("Indiana", "Pacers"), "LAC": ("Los Angeles", "Clippers"),
    "LAL": ("Los Angeles", "Lakers"), "MEM": ("Memphis", "Grizzlies"),
    "MIA": ("Miami", "Heat"), "MIL": ("Milwaukee", "Bucks"),
    "MIN": ("Minnesota", "Timberwolves"), "NOP": ("New Orleans", "Pelicans"),
    "NYK": ("New York", "Knicks"), "OKC": ("Oklahoma City", "Thunder"),
    "ORL": ("Orlando", "Magic"), "PHI": ("Philadelphia", "76ers"),
    "PHX": ("Phoenix", "Suns"), "POR": ("Portland", "Trail Blazers"),
    "SAC": ("Sacramento", "Kings"), "SAS": ("San Antonio", "Spurs"),
    "TOR": ("Toronto", "Raptors"), "UTA": ("Utah", "Jazz"),
    "WAS": ("Washington", "Wizards"),
}


class Masker:
    def __init__(self, salt: str, enabled: bool = True, epoch: date | None = None) -> None:
        self.enabled = enabled
        self.salt = salt
        self._key = salt.encode()
        self._epoch = epoch or date(2000, 1, 1)
        self._players: set[str] = set()
        self._reverse: dict[str, str] = {}
        terms: list[str] = []
        for tri, (city, nick) in NBA_TEAMS.items():
            terms += [f"{city} {nick}", nick, tri]
            if city not in ("Los Angeles", "New York"):  # cities shared/ambiguous
                terms.append(city)
        terms += ["Los Angeles", "New York"]
        self._terms = sorted(set(terms), key=len, reverse=True)

    def _token(self, kind: str, value: str) -> str:
        digest = hmac.new(self._key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()
        tok = f"{kind}-{digest[:6]}"
        self._reverse[tok] = value
        return tok

    def team(self, tricode: str) -> str:
        return self._token("team", tricode) if self.enabled else tricode

    def player(self, name: str) -> str:
        if not self.enabled:
            return name
        self._players.add(name)
        return self._token("player", name)

    def date(self, d: date | datetime) -> str:
        if not self.enabled:
            return d.isoformat()
        d = d.date() if isinstance(d, datetime) else d
        return f"day-{(d - self._epoch).days}"

    def text(self, s: str) -> str:
        """Scrub free text (e.g. a hint or note) of any team/player names."""
        if not self.enabled:
            return s
        for tri, (city, nick) in NBA_TEAMS.items():
            tok = self.team(tri)
            s = re.sub(rf"\b{re.escape(city)} {re.escape(nick)}\b", tok, s)
            s = re.sub(rf"\b{re.escape(nick)}\b", tok, s)
            s = re.sub(rf"\b{tri}\b", tok, s)
        for p in sorted(self._players, key=len, reverse=True):
            s = s.replace(p, self.player(p))
        return s

    def leaked_terms(self, s: str) -> list[str]:
        """Real names that appear in `s` (for tests and runtime assertions)."""
        found = []
        for term in self._terms:
            # Case-sensitive: team names are proper nouns, and ordinary words ("nets out",
            # "heat", "magic", "thunder") must not count as leaks.
            if re.search(rf"\b{re.escape(term.strip())}\b", s):
                found.append(term.strip())
        return found + [p for p in self._players if p in s]

    def unmask(self, token: str) -> str:
        return self._reverse.get(token, token)
