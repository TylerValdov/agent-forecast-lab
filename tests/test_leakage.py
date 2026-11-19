"""Adversarial tests for the as_of guard. If any of these can read the future, the build is
broken. (Synthetic data only.)"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from forecast_lab.data.masker import Masker
from forecast_lab.data.schema import InjuryEntry, InjurySnapshot
from forecast_lab.data.store import AsOfError, DataStore, RevealError
from forecast_lab.tools.nba_tools import NBAToolbox

D = datetime(2030, 1, 10, tzinfo=UTC)


def ids(rows):
    return {r.game_id for r in rows}


def test_own_result_never_visible_before_or_at_tipoff(tiny_store):
    g3 = tiny_store.scheduled("G3")
    for as_of in [g3.tipoff_utc - timedelta(hours=1), g3.tipoff_utc,
                  g3.tipoff_utc + timedelta(hours=3)]:
        assert "G3" not in ids(tiny_store.results(as_of))
        assert "G3" not in ids(tiny_store.results(as_of, team="BOS"))
        assert "G3" not in ids(tiny_store.team_box(as_of, "MIA"))


def test_boundary_is_strict(tiny_store):
    g1 = tiny_store.reveal("G1", clock=D + timedelta(days=30))
    assert "G1" not in ids(tiny_store.results(g1.available_at))
    assert "G1" in ids(tiny_store.results(g1.available_at + timedelta(microseconds=1)))


def test_same_day_games_finished_before_vs_after(tiny_store):
    # G1 tipped 17:00 (available 20:30). G2 tipped 23:00, the same day as nothing yet.
    as_of = D + timedelta(hours=22)  # after G1 is final, before G2 starts
    seen = ids(tiny_store.results(as_of))
    assert "G1" in seen and "G2" not in seen
    as_of = D + timedelta(hours=23, minutes=30)  # G2 in progress: still hidden
    assert "G2" not in ids(tiny_store.results(as_of))


def test_reveal_before_available_raises(tiny_store):
    tip = tiny_store.scheduled("G3").tipoff_utc
    with pytest.raises(RevealError):
        tiny_store.reveal("G3", clock=tip + timedelta(hours=1))
    assert tiny_store.reveal("G3", clock=tip + timedelta(hours=4)).home_pts == 95


@pytest.mark.parametrize("bad", [None, "2030-01-10", date(2030, 1, 10),
                                 datetime(2030, 1, 10, 12), 1_900_000_000])
def test_invalid_as_of_rejected(tiny_store, bad):
    with pytest.raises(AsOfError):
        tiny_store.results(bad)
    with pytest.raises(AsOfError):
        tiny_store.team_box(bad, "BOS")
    with pytest.raises(AsOfError):
        tiny_store.latest_injury_report(bad, "BOS")


def test_returned_records_are_immutable_and_copied(tiny_store):
    as_of = D + timedelta(days=30)
    rows = tiny_store.results(as_of)
    n = len(rows)
    rows.clear()
    assert len(tiny_store.results(as_of)) == n
    with pytest.raises(ValidationError):
        tiny_store.results(as_of)[0].home_pts = 0  # type: ignore[misc]


def test_injury_reports_after_as_of_hidden():
    tip = D + timedelta(hours=23)
    snaps = [
        InjurySnapshot(team="BOS", published_at=tip - timedelta(hours=6), source="t",
                       entries=(InjuryEntry(player="Player A", status="Questionable"),)),
        InjurySnapshot(team="BOS", published_at=tip - timedelta(minutes=30), source="t",
                       entries=(InjuryEntry(player="Player A", status="Out"),)),
    ]
    store = DataStore(results=[], schedule=[], injuries=snaps)
    as_of = tip - timedelta(hours=1)
    assert store.latest_injury_report(as_of, "BOS").entries[0].status == "Questionable"
    # A report published exactly at as_of is not visible either.
    assert store.latest_injury_report(snaps[1].published_at, "BOS").entries[0].status == \
        "Questionable"
    assert store.latest_injury_report(tip, "BOS").entries[0].status == "Out"
    assert store.latest_injury_report(tip - timedelta(hours=7), "BOS") is None


def test_naive_timestamps_rejected_at_construction():
    with pytest.raises(ValidationError):
        InjurySnapshot(team="BOS", published_at=datetime(2030, 1, 1), entries=(), source="t")


def test_tools_cannot_see_own_or_future_results(tiny_store):
    g3 = tiny_store.scheduled("G3")
    as_of = g3.tipoff_utc - timedelta(hours=1)
    tb = NBAToolbox(tiny_store, g3, as_of, Masker("s", enabled=False), injuries_enabled=False)
    outputs = [
        tb.execute("get_team_form", {"team": "BOS", "last_n": 20})[0],
        tb.execute("get_team_form", {"team": "MIA", "last_n": 20})[0],
        tb.execute("get_rest_days", {"team": "BOS"})[0],
        tb.execute("get_head_to_head", {"team_a": "BOS", "team_b": "MIA", "last_n": 20})[0],
    ]
    blob = " ".join(outputs)
    assert "95-130" not in blob and "130-95" not in blob  # G3's own score
    h2h = json.loads(outputs[3])["meetings"]
    assert len(h2h) == 1  # only G0, not G3
    # G2 (MIA's game the previous night) finished before as_of: it should be visible.
    assert json.loads(outputs[1])["summary"]["record"] == "1-1"  # lost G0, won G2


def test_tools_only_accept_the_two_teams_in_the_game(tiny_store):
    g3 = tiny_store.scheduled("G3")
    tb = NBAToolbox(tiny_store, g3, g3.tipoff_utc, Masker("s", enabled=False), False)
    out, is_error = tb.execute("get_team_form", {"team": "NYK", "last_n": 5})
    assert is_error and "Unknown team" in out


def test_anonymized_tool_outputs_leak_no_real_names(tiny_store):
    g3 = tiny_store.scheduled("G3")
    masker = Masker("salt", enabled=True)
    tb = NBAToolbox(tiny_store, g3, g3.tipoff_utc, masker, injuries_enabled=False)
    home, away = masker.team("BOS"), masker.team("MIA")
    outs = [
        tb.execute("get_team_form", {"team": home, "last_n": 20})[0],
        tb.execute("get_team_form", {"team": away, "last_n": 20})[0],
        tb.execute("get_rest_days", {"team": home})[0],
        tb.execute("get_head_to_head", {"team_a": home, "team_b": away, "last_n": 5})[0],
    ]
    for out in outs:
        assert json.loads(out).get("error") is None
        assert masker.leaked_terms(out) == [], out
        assert not re.search(r"\b20\d\d-\d\d-\d\d\b", out)  # no calendar dates


def test_leak_check_ignores_ordinary_words():
    m = Masker("salt")
    assert m.leaked_terms("home court plus noisy margins nets out near 3; heat check") == []
    assert m.leaked_terms("the Nets won") == ["Nets"]
    assert m.leaked_terms("magic number, thunder, kings of the hill") == []


def test_masker_scrubs_free_text():
    m = Masker("salt")
    text = "The Boston Celtics beat the Knicks; LAL rested. Player A is out."
    m.player("Player A")
    scrubbed = m.text(text)
    assert m.leaked_terms(scrubbed) == []
    assert m.team("BOS") in scrubbed


def test_no_code_outside_store_touches_private_state():
    """Static check: the only door into the data is the DataStore's public API."""
    private = re.compile(r"\._(results|results_by_id|team_results|team_box|team_injuries|"
                         r"schedule)\b")
    src = Path(__file__).resolve().parents[1] / "src" / "forecast_lab"
    offenders = [
        str(p) for p in src.rglob("*.py")
        if p.name != "store.py" and private.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
