"""An intervals.icu sync does not erase what only Domestique knows.

`activities` holds two kinds of column: what intervals.icu sent (name, sport,
duration, TSS, power) and what Domestique decided locally. `is_race` is the
second kind -- the rider marks a ride as a race, and the tau fitting and the
race-week logic read that flag afterwards.

The sync upserts with `INSERT OR REPLACE`, and its column list does not
include `is_race`. In SQLite, INSERT OR REPLACE on a primary-key conflict is a
DELETE followed by an INSERT, not an UPDATE: every column the statement omits
goes back to its schema default, so `is_race` returned to 0 on every sync, for
every ride, silently. The flag survived only until the next sync ran.

The rule: a sync carries intervals.icu's facts and leaves the local ones
alone.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import db                                             # noqa: E402

ACTIVITY_ID = "i5150"
ICU_PAYLOAD = [{
    "id": ACTIVITY_ID,
    "start_date_local": "2026-09-14T09:00:00",
    "name": "Local road race",
    "type": "Ride",
    "moving_time": 5100,
    "icu_training_load": 96,
    "icu_average_watts": 178,
    "average_heartrate": 148,
    "distance": 42000,
}]


@pytest.fixture()
def fresh_db(tmp_path):
    original = db.DB_PATH
    db.set_db_path(tmp_path / "health_tracker.db")
    db.close_all_connections()
    db.init_db()
    try:
        yield
    finally:
        db.set_db_path(original)
        db.close_all_connections()


def _sync():
    with patch.object(db, "fetch_activities", return_value=ICU_PAYLOAD):
        return db.sync_activities(days=30)


def _is_race() -> int:
    row = db.get_db().execute(
        "SELECT is_race FROM activities WHERE id = ?", (ACTIVITY_ID,)).fetchone()
    return None if row is None else row["is_race"]


def test_a_race_flag_survives_the_next_sync(fresh_db):
    _sync()
    assert db._set_is_race(ACTIVITY_ID, True) is True
    assert _is_race() == 1, "the flag did not persist in the first place"

    _sync()                                  # the same ride comes round again
    assert _is_race() == 1, (
        "the sync cleared is_race: INSERT OR REPLACE deletes the row and "
        "re-inserts it, so every column the statement omits goes back to its "
        "default, and the rider's race became an ordinary ride")


WELLNESS_DAY = "2026-09-15"


def _sync_wellness(rows):
    with patch.object(db, "fetch_wellness", return_value=rows):
        return db.sync_wellness(days=30)


def test_a_hand_typed_hrv_survives_a_sync_that_has_none(fresh_db):
    """The rider takes their own HRV reading on a morning intervals.icu knows
    nothing about. The sync must not treat silence as an answer."""
    _sync_wellness([{"id": WELLNESS_DAY, "ctl": 40.0, "atl": 30.0}])
    db.get_db().execute("UPDATE wellness SET hrv = ? WHERE date = ?",
                        (62.0, WELLNESS_DAY))
    db.get_db().commit()

    _sync_wellness([{"id": WELLNESS_DAY, "ctl": 41.0, "atl": 31.0}])   # no hrv
    row = db.get_db().execute(
        "SELECT hrv, ctl FROM wellness WHERE date = ?", (WELLNESS_DAY,)).fetchone()
    assert row["hrv"] == 62.0, (
        "the sync erased the rider's own HRV reading: INSERT OR REPLACE "
        "re-inserts the row, so a field intervals.icu does not have goes back "
        "to NULL")
    assert row["ctl"] == 41.0, "intervals.icu's own numbers must still update"


def test_intervals_icu_wins_when_it_does_have_the_reading(fresh_db):
    """The guard on the guard: preserving local values must not freeze ICU's."""
    _sync_wellness([{"id": WELLNESS_DAY, "hrv": 55.0}])
    _sync_wellness([{"id": WELLNESS_DAY, "hrv": 71.0}])
    row = db.get_db().execute(
        "SELECT hrv FROM wellness WHERE date = ?", (WELLNESS_DAY,)).fetchone()
    assert row["hrv"] == 71.0


def test_the_sync_still_updates_what_intervals_icu_owns(fresh_db):
    """The guard must not turn into 'never overwrite anything'."""
    _sync()
    db._set_is_race(ACTIVITY_ID, True)
    changed = [dict(ICU_PAYLOAD[0], name="Local road race (official results)",
                    icu_training_load=104)]
    with patch.object(db, "fetch_activities", return_value=changed):
        db.sync_activities(days=30)
    row = db.get_db().execute(
        "SELECT name, tss, is_race FROM activities WHERE id = ?",
        (ACTIVITY_ID,)).fetchone()
    assert row["name"] == "Local road race (official results)"
    assert row["tss"] == 104
    assert row["is_race"] == 1


def test_readings_intervals_icu_stops_sending_survive(fresh_db):
    """#29: intervals.icu may stop passing Garmin wellness on through its API.
    Then the 90-day sync returns days it sent HRV, resting HR and sleep for
    with those fields empty, and the stored history must not be wiped."""
    _sync_wellness([{"id": WELLNESS_DAY, "hrv": 62.0, "restingHR": 44,
                     "sleepSecs": 27000, "ctl": 40.0}])
    _sync_wellness([{"id": WELLNESS_DAY, "ctl": 40.5}])   # Garmin fields withheld
    row = db.get_db().execute(
        "SELECT hrv, rhr, sleep_secs, ctl FROM wellness WHERE date = ?",
        (WELLNESS_DAY,)).fetchone()
    assert tuple(row) == (62.0, 44, 27000, 40.5)


def test_a_zero_reading_is_no_reading(fresh_db):
    """intervals.icu (or the device behind it) can send 0 for a reading it does
    not have. Nobody has an HRV, a resting HR or a night's sleep of zero, and
    every reader treats 0 as missing, so a 0 must not overwrite a value."""
    _sync_wellness([{"id": WELLNESS_DAY, "hrv": 62.0, "restingHR": 44,
                     "sleepSecs": 27000, "sleepScore": 81}])
    _sync_wellness([{"id": WELLNESS_DAY, "hrv": 0, "restingHR": 0,
                     "sleepSecs": 0, "sleepScore": 0}])
    row = db.get_db().execute(
        "SELECT hrv, rhr, sleep_secs, sleep_score FROM wellness WHERE date = ?",
        (WELLNESS_DAY,)).fetchone()
    assert tuple(row) == (62.0, 44, 27000, 81)


def test_an_icu_hrv_kept_through_a_zero_stays_icus(fresh_db):
    """The manual-HRV guard reads "typed" as an HRV raw_json does not carry.
    An ICU HRV kept through a record with hrv 0 must stay ICU's, so the next
    real reading still replaces it; a typed HRV stays the rider's."""
    _sync_wellness([{"id": WELLNESS_DAY, "hrv": 62.0}])
    _sync_wellness([{"id": WELLNESS_DAY, "hrv": 0}])
    _sync_wellness([{"id": WELLNESS_DAY, "hrv": 71.0}])
    row = db.get_db().execute(
        "SELECT hrv FROM wellness WHERE date = ?", (WELLNESS_DAY,)).fetchone()
    assert row["hrv"] == 71.0, "a 0 turned intervals.icu's HRV into a typed one"

    db.get_db().execute("UPDATE wellness SET hrv = ? WHERE date = ?",
                        (58.0, WELLNESS_DAY))
    db.get_db().commit()
    _sync_wellness([{"id": WELLNESS_DAY, "hrv": 0}])
    _sync_wellness([{"id": WELLNESS_DAY, "hrv": 66.0}])
    row = db.get_db().execute(
        "SELECT hrv FROM wellness WHERE date = ?", (WELLNESS_DAY,)).fetchone()
    assert row["hrv"] == 58.0, "the rider's typed HRV was replaced"


# The day's JSON file is the second wellness store: readiness, the resting-HR
# baseline and /api/wellness read it. The app's own wellness sync writes it
# through persist_wellness, which replaced the whole file each time.

def test_the_wellness_file_keeps_readings_a_sync_stops_sending(tmp_path):
    import json
    import ride_storage as rs
    with patch.object(rs, "_wellness_dir", return_value=tmp_path):
        rs.persist_wellness({"id": WELLNESS_DAY, "hrv": 60.0, "restingHR": 44,
                             "sleepSecs": 27000, "ctl": 40.0,
                             "sportInfo": [{"type": "Ride", "eftp": 250}]})
        # Garmin fields withheld: null, zero, an empty list, then a bare day.
        rs.persist_wellness({"id": WELLNESS_DAY, "hrv": None, "restingHR": 0,
                             "sleepSecs": None, "ctl": 40.5, "sportInfo": []})
        rs.persist_wellness({"id": WELLNESS_DAY})
        day = json.loads((tmp_path / f"{WELLNESS_DAY}.json").read_text())
        assert (day["hrv"], day["restingHR"], day["sleepSecs"]) == (60.0, 44, 27000)
        assert day["sportInfo"] == [{"type": "Ride", "eftp": 250}]
        assert day["ctl"] == 40.5, "intervals.icu's own numbers must still update"
        # and a real new reading still replaces the stored one
        rs.persist_wellness({"id": WELLNESS_DAY, "hrv": 71.0})
        day = json.loads((tmp_path / f"{WELLNESS_DAY}.json").read_text())
        assert day["hrv"] == 71.0 and day["restingHR"] == 44
