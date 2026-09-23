"""A ride record has one writer path: locked, atomic, read-modify-write
(contract 2.2).

The audit (2026-09-23, data #2) found every ride-record writer did its own
read, change and `write_text`, with no lock. The one it reproduced end to
end: persist_icu_activity writes the record, computes the ride's PRs, then
writes the WHOLE record again from its own copy -- so an RPE the rider saved
in between was erased by the second write. The DFA write-back in app.py does
the same across up to 45 s of intervals.icu fetches. And `write_text`
truncates before it writes, so a reader in that instant (or a crash) got an
empty or torn file, and the next sync dropped RPE, verdict, laps and DFA.

ride_storage.update_ride(ride_id, mutator) is the one path: a per-record
lock over read -> mutate -> temp file -> os.replace.
"""
from __future__ import annotations

import datetime
import json
import threading
from unittest import mock

import pytest

import db
import power_curve
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
    db._sync_stop.clear()
    yield pm
    p.stop()
    ProfileManager._instance = old
    db.set_db_path(old_db)


EXT = "i9001"
ACT = {"id": EXT, "start_date_local": f"{datetime.date.today()}T08:00:00",
       "type": "Ride", "name": "Morning", "moving_time": 3600,
       "elapsed_time": 3700, "icu_training_load": 60, "average_watts": 200,
       "distance": 30000, "source": "GARMIN_CONNECT"}


def test_concurrent_updates_all_land_and_no_reader_sees_a_torn_file(pm):
    path = rs.persist_icu_activity(dict(ACT))
    rs.update_ride(f"icu_{EXT}", lambda r: {**r, "n": 0})
    torn, stop = [], threading.Event()

    def reader():
        while not stop.is_set():
            try:
                json.loads(path.read_text())
            except (OSError, ValueError) as e:
                torn.append(repr(e))

    def bump():
        for _ in range(50):
            rs.update_ride(EXT, lambda r: {**r, "n": r["n"] + 1})
    r = threading.Thread(target=reader)
    r.start()
    ts = [threading.Thread(target=bump) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    stop.set()
    r.join()
    assert json.loads(path.read_text())["n"] == 400, "updates were lost"
    assert torn == [], f"{len(torn)} torn reads: {torn[:2]}"
    assert not [p for p in path.parent.iterdir() if p.suffix == ".tmp"]


def test_an_rpe_saved_while_a_sync_persist_is_in_flight_survives(pm, monkeypatch):
    """The audit's reproduction (rv-data p5, WRITER=persist), with the rating
    posted through the real endpoint."""
    from fastapi.testclient import TestClient
    import app as app_module
    rs.persist_icu_activity(dict(ACT))
    paused, release = threading.Event(), threading.Event()

    def slow_prs(ride_id):
        paused.set()
        release.wait(20)
        return []
    monkeypatch.setattr(power_curve, "compute_ride_prs", slow_prs)
    t = threading.Thread(target=rs.persist_icu_activity,
                         args=(dict(ACT, name="Morning (edited on ICU)"),))
    t.start()
    assert paused.wait(20)
    c = TestClient(app_module.app)
    assert c.post(f"/api/ride/icu_{EXT}/rpe", json={"rpe": 7}).status_code == 200
    release.set()
    t.join(30)
    rec = rs.get_icu_ride(EXT)
    assert rec["rpe"] == 7, "the sync's second write erased the rider's RPE"
    assert rec["name"] == "Morning (edited on ICU)"
    assert rec["prs"] == []


def test_a_failed_write_leaves_the_record_as_it_was(pm, monkeypatch):
    path = rs.persist_icu_activity(dict(ACT))
    before = path.read_text()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(rs.os, "replace", boom)
    with pytest.raises(OSError):
        rs.update_ride(EXT, lambda r: {**r, "rpe": 5})
    assert path.read_text() == before
    assert not [p for p in path.parent.iterdir() if p.suffix == ".tmp"]


def test_a_mutator_returning_none_writes_nothing(pm):
    path = rs.persist_icu_activity(dict(ACT))
    before = path.stat().st_mtime_ns
    assert rs.update_ride(EXT, lambda r: None) is None
    assert path.stat().st_mtime_ns == before


def test_unknown_or_malformed_ids_write_nothing(pm):
    assert rs.update_ride("icu_i404", lambda r: {"x": 1}) is None
    assert not list(rs._icu_rides_dir().glob("*.json"))
    for bad in ("../../x", "", None, "icu_a/b", "fit_../x"):
        assert rs.update_ride(bad, lambda r: {"x": 1}) is None


def test_a_fit_ride_updates_its_load_sidecar(pm):
    fit = rs._fit_rides_dir() / "2026-09-20T08-00-00.fit"
    fit.write_bytes(b"\x0e\x10")
    side = rs._fit_load_sidecar_path(fit)
    side.write_text(json.dumps({"tss": 50, "reader_v": rs.FIT_LOAD_READER_V}))
    out = rs.update_ride("fit_2026-09-20T08-00-00", lambda r: {**r, "rpe": 6})
    assert out["rpe"] == 6 and json.loads(side.read_text())["tss"] == 50
