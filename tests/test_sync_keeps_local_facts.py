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
