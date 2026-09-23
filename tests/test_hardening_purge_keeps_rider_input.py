"""Disconnecting intervals.icu keeps what the rider typed (contract 2.1).

The audit (2026-09-23, data #1) reproduced it: /api/icu/disconnect and
"switch account" call db.purge_profile_data, which rmtree'd rides/icu/ and
emptied `activities` and `wellness`. The rider's RPE, their FTP-test verdict,
their race flags and the HRV they typed in went with the mirror, and none of
it came back on reconnect -- while the docstring said "Manual values are
never touched".

Where rider input lives now (the minimal design): on its ride / day as
before, and when the mirror is removed, in the profile's
`rider_input.json` (ride keys, by intervals.icu id) or left in its
`wellness` row (manual HRV) -- then put back when the same ride / day is
synced again.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

import db
import ride_storage as rs
from profile_manager import ProfileManager


@pytest.fixture
def pm(tmp_path):
    p = mock.patch("pathlib.Path.home", return_value=tmp_path)
    p.start()
    old = ProfileManager._instance
    ProfileManager._instance = None
    old_db = db.DB_PATH
    pm = ProfileManager.get()
    pm.switch(pm.create_profile("Alpha"))
    db.shutdown_sync()
    yield pm
    p.stop()
    ProfileManager._instance = old
    db.set_db_path(old_db)
    db.close_all_connections()


ACT = {"id": "i55", "start_date_local": "2026-09-21T08:00:00", "type": "Ride",
       "name": "Tempo", "moving_time": 3600, "icu_training_load": 70,
       "average_watts": 210, "source": "GARMIN_CONNECT"}
OTHER = dict(ACT, id="i56", start_date_local="2026-09-20T08:00:00", name="Easy")
D_ICU_NO_HRV = "2026-09-21"      # intervals.icu has the day, but no HRV
D_MANUAL_ONLY = "2026-09-22"     # only the rider's typed HRV
WELLNESS = [{"id": D_ICU_NO_HRV, "ctl": 60, "atl": 55, "sportInfo": []},
            {"id": "2026-09-20", "ctl": 59, "atl": 54, "hrv": 70, "sportInfo": []}]
REVIEW = {"action": "accepted", "ftp": 281, "at": "2026-09-21T10:00:00"}


def _manual_hrv(day, rmssd):
    """What /api/wellness/manual-hrv does (app.py)."""
    conn = db.get_db()
    if conn.execute("SELECT 1 FROM wellness WHERE date = ?", (day,)).fetchone():
        conn.execute("UPDATE wellness SET hrv = ? WHERE date = ?", (rmssd, day))
    else:
        conn.execute("INSERT INTO wellness (date, ctl, atl, hrv, rhr, sleep_secs, "
                     "sleep_score, eftp, raw_json) VALUES (?, NULL, NULL, ?, NULL, "
                     "NULL, NULL, NULL, '{}')", (day, rmssd))
    conn.commit()


def _sync():
    with mock.patch.object(db, "fetch_activities", lambda days=90: [dict(ACT), dict(OTHER)]), \
         mock.patch.object(db, "fetch_wellness", lambda days=90: [dict(w) for w in WELLNESS]):
        db.sync_wellness(90)
        db.sync_activities(90)
    for a in (ACT, OTHER):
        rs.persist_icu_activity(dict(a))


def _state():
    rec = rs.get_icu_ride("i55") or {}
    conn = db.get_db()
    race = conn.execute("SELECT is_race FROM activities WHERE id='i55'").fetchone()
    hrv = {d: (conn.execute("SELECT hrv FROM wellness WHERE date=?", (d,)).fetchone() or [None])[0]
           for d in (D_ICU_NO_HRV, D_MANUAL_ONLY)}
    return {"rpe": rec.get("rpe"), "rpe_at": rec.get("rpe_at"),
            "rpe_scale": rec.get("rpe_scale"),
            "ftp_test_review": rec.get("ftp_test_review"),
            "is_race": race and race[0], "hrv": hrv}


EXPECTED = {"rpe": 8, "rpe_at": "2026-09-21T12:00:00", "rpe_scale": "foster_cr10",
            "ftp_test_review": REVIEW, "is_race": 1,
            "hrv": {D_ICU_NO_HRV: 61.0, D_MANUAL_ONLY: 58.0}}


def _rider_types_things():
    rs.update_ride("icu_i55", lambda r: {**r, "rpe": 8, "rpe_at": "2026-09-21T12:00:00",
                                         "rpe_scale": "foster_cr10",
                                         "ftp_test_review": REVIEW})
    assert db._set_is_race("i55", True)
    _manual_hrv(D_ICU_NO_HRV, 61.0)
    _manual_hrv(D_MANUAL_ONLY, 58.0)


def test_disconnect_then_reconnect_brings_every_rider_value_back(pm):
    _sync()
    _rider_types_things()
    assert _state() == EXPECTED
    removed = db.purge_profile_data(pm.active_id)
    # the mirror IS gone
    assert rs.get_icu_ride("i55") is None
    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM wellness WHERE ctl IS NOT NULL").fetchone()[0] == 0
    assert removed["activities"] == 2
    # a reconnect and a sync
    _sync()
    assert _state() == EXPECTED
    other = rs.get_icu_ride("i56")
    assert other.get("rpe") is None and other.get("ftp_test_review") is None


def test_the_rider_input_waits_in_the_profile_until_the_ride_returns(pm):
    _sync()
    _rider_types_things()
    db.purge_profile_data(pm.active_id)
    stash = json.loads((pm.active_dir / "rider_input.json").read_text())
    assert stash["rides"]["i55"]["rpe"] == 8
    assert stash["rides"]["i55"]["is_race"] == 1
    _sync()
    stash = json.loads((pm.active_dir / "rider_input.json").read_text())
    assert stash["rides"] == {}, "restored input must not be applied twice"


def test_a_second_purge_before_reconnect_keeps_the_first_stash(pm):
    _sync()
    _rider_types_things()
    db.purge_profile_data(pm.active_id)
    db.purge_profile_data(pm.active_id)
    _sync()
    assert _state() == EXPECTED


def test_purge_strips_the_icu_values_and_keeps_the_typed_hrv(pm):
    _sync()
    _manual_hrv(D_MANUAL_ONLY, 58.0)
    db.purge_profile_data(pm.active_id)
    rows = db.get_db().execute("SELECT date, ctl, atl, hrv, raw_json FROM wellness").fetchall()
    assert [tuple(r) for r in rows] == [(D_MANUAL_ONLY, None, None, 58.0, "{}")]


def test_through_the_disconnect_endpoint(pm, monkeypatch):
    """The route the rider clicks: /api/icu/disconnect (the audit's p7)."""
    from fastapi.testclient import TestClient
    import app as app_module
    import icu_calendar_push
    monkeypatch.setattr(icu_calendar_push, "sweep_all", lambda *a, **k: None)
    pm.save_icu_token("fake-token", "i123")
    _sync()
    _rider_types_things()
    r = TestClient(app_module.app).post("/api/icu/disconnect")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert rs.get_icu_ride("i55") is None
    pm.save_icu_token("fake-token", "i123")       # reconnect, same account
    _sync()
    assert _state() == EXPECTED


# ── review findings (2026-09-23) ─────────────────────────────────────────────

def _sync_wellness(records):
    with mock.patch.object(db, "fetch_wellness", lambda days=90: [dict(r) for r in records]):
        db.sync_wellness(90)


def _hrv(day):
    row = db.get_db().execute("SELECT hrv FROM wellness WHERE date=?", (day,)).fetchone()
    return row and row[0]


def test_an_icu_hrv_kept_by_a_later_hrv_less_record_is_still_icus(pm):
    """intervals.icu sends HRV 55, later re-sends the day without one: the
    sync keeps 55 (silence is not an answer) and it is still ICU's -- a purge
    removes it, it is not parked as if the rider typed it."""
    day = "2026-09-19"
    _sync_wellness([{"id": day, "ctl": 50, "atl": 45, "hrv": 55, "sportInfo": []}])
    _sync_wellness([{"id": day, "ctl": 51, "atl": 46, "sportInfo": []}])
    assert _hrv(day) == 55
    db.purge_profile_data(pm.active_id)
    assert _hrv(day) is None


def test_a_typed_hrv_over_an_icu_one_survives_sync_and_reconnect(pm):
    """Contract 2.1: typed HRV is back on its day after a reconnect and a
    sync -- also on a day intervals.icu has its own HRV for."""
    day = "2026-09-14"
    icu_day = [{"id": day, "ctl": 50, "atl": 45, "hrv": 72, "sportInfo": []}]
    _sync_wellness(icu_day)
    _manual_hrv(day, 80.0)
    _sync_wellness(icu_day)
    assert _hrv(day) == 80.0, "the next sync overwrote what the rider typed"
    db.purge_profile_data(pm.active_id)
    _sync_wellness(icu_day)
    assert _hrv(day) == 80.0
