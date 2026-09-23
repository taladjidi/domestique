"""athlete.json is written safely and never silently reset (contract 3.1, 3.2).

The audit (2026-09-23, data #6) found two failures in one helper:

3.1 Every profile JSON was written through a FIXED temp name,
    `athlete.json.tmp`. Two writers at once (the sync thread's LTHR mirror and
    a request recording an FTP test) shared it: one os.replace moved the
    other's temp file away, the second raised FileNotFoundError (a 500), and
    json.dumps of the shared dict could raise "dictionary changed size during
    iteration" while the other thread updated it.

3.2 `_load_json` turned an unparseable file into `{}`. The next write -- the
    sync thread mirrors LTHR every 30 minutes -- then persisted only its own
    keys over the file: FTP, weight and the FTP-test history gone, no backup.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from unittest import mock

import pytest

import db
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


N = 200


def test_concurrent_writers_and_a_reader(pm):
    pm.save_athlete({"ftp": 250, "weight_kg": 70, "lthr": 165})
    path = pm.active_dir / "athlete.json"
    errors = {"A": [], "B": [], "torn": 0, "reads": 0}
    stop = threading.Event()

    def writer_a():                      # the sync thread's mirror
        for i in range(N):
            try:
                pm.save_athlete({"lthr": 150 + (i % 50), "weight_kg": 70 + (i % 5)})
            except Exception as e:       # noqa: BLE001 — the failure under test
                errors["A"].append(repr(e))

    def writer_b():                      # a request recording tests
        for i in range(N):
            try:
                pm.record_ftp_test("ramp", 250 + (i % 10), source=f"t{i}")
            except Exception as e:       # noqa: BLE001
                errors["B"].append(repr(e))

    def reader():
        while not stop.is_set():
            errors["reads"] += 1
            try:
                if "ftp" not in json.loads(path.read_text()):
                    errors["torn"] += 1
            except Exception:            # noqa: BLE001 — a torn or missing file
                errors["torn"] += 1

    r = threading.Thread(target=reader)
    ws = [threading.Thread(target=f) for f in (writer_a, writer_b)]
    r.start()
    for t in ws:
        t.start()
    for t in ws:
        t.join()
    stop.set()
    r.join()
    assert errors["A"] == [] and errors["B"] == [], (errors["A"][:3], errors["B"][:3])
    assert errors["torn"] == 0, f"reader saw {errors['torn']}/{errors['reads']} torn reads"
    on_disk = json.loads(path.read_text())
    assert len(on_disk["ftp_test_history"]) == N
    assert on_disk["ftp"] == 250
    assert not list(path.parent.glob("athlete.json.*tmp*")), "a temp file was left behind"


def test_two_concurrent_test_recorders_keep_every_entry_on_disk(pm, monkeypatch):
    """Without one lock over snapshot + write, a writer whose snapshot is
    older can land last: the file on disk loses entries memory still has.
    A disk with variable fsync latency (modelled here) lets writes overtake."""
    import os
    import random
    import time as _time
    import profile_manager
    real_fsync = os.fsync

    def slow_fsync(fd):
        _time.sleep(random.random() * 0.003)
        return real_fsync(fd)
    monkeypatch.setattr(profile_manager.os, "fsync", slow_fsync)
    pm.save_athlete({"ftp": 250, "weight_kg": 70})
    path = pm.active_dir / "athlete.json"
    n = 60

    def rec(tag):
        for i in range(n):
            pm.record_ftp_test("ramp", 250, source=f"{tag}{i}")
    seen = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                seen.append(len(json.loads(path.read_text())["ftp_test_history"]))
            except (OSError, ValueError, KeyError):
                pass
            _time.sleep(0.0005)
    r = threading.Thread(target=reader)
    r.start()
    ts = [threading.Thread(target=rec, args=(t,)) for t in "ab"]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    stop.set()
    r.join()
    shrank = [(a, b) for a, b in zip(seen, seen[1:]) if b < a]
    assert not shrank, f"the file on disk went back in time {len(shrank)}x: {shrank[:5]}"
    on_disk = json.loads(path.read_text())["ftp_test_history"]
    assert len(pm.ftp_test_history) == 2 * n
    assert len(on_disk) == 2 * n, f"disk kept {len(on_disk)} of {2 * n}"


def test_the_temp_file_is_unique_per_write(pm):
    """The .env write goes through _write_text_atomic without the JSON lock;
    a shared `<name>.tmp` made concurrent writers steal each other's temp."""
    path = pm.active_dir / "probe.txt"
    errs = []

    def w(tag):
        for i in range(N):
            try:
                pm._write_text_atomic(path, f"{tag}{i}\n" * 50)
            except Exception as e:           # noqa: BLE001 — the failure under test
                errs.append(repr(e))
    ts = [threading.Thread(target=w, args=(t,)) for t in "ab"]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errs == [], f"{len(errs)} of {2 * N} writes failed: {errs[:2]}"
    assert not list(path.parent.glob("probe.txt.*"))


def _torn(pm):
    pm.save_athlete({"ftp": 287, "weight_kg": 71.5, "lthr": 168, "age": 41, "sex": "M"})
    for i in range(3):
        pm.record_ftp_test("ramp", 280 + i, source=f"test{i}", applied=True)
    path = pm.active_dir / "athlete.json"
    full = path.read_text()
    torn = full[: len(full) // 2]
    path.write_text(torn)
    return path, torn


def test_a_corrupt_athlete_json_is_moved_aside_and_logged(pm, caplog):
    path, torn = _torn(pm)
    with caplog.at_level(logging.WARNING, logger="domestique.profiles"):
        pm._load_active_profile()               # what boot and a switch do
    asides = list(path.parent.glob("athlete.json.corrupt-*"))
    assert len(asides) == 1, [p.name for p in path.parent.iterdir()]
    assert asides[0].read_text() == torn, "the rider's bytes must be kept as they were"
    assert any("corrupt" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_a_partial_write_after_corruption_does_not_become_the_riders_file(pm):
    path, torn = _torn(pm)
    pm._load_active_profile()
    # the sync thread's HR mirror (db._refresh_hr_from_activities) writes this
    pm.save_athlete({"lthr": 170, "lthr_source": "icu", "lthr_source_date": "2026-09-23"})
    if path.exists():
        d = json.loads(path.read_text())
        assert "ftp" in d and "weight_kg" in d, f"near-empty file persisted: {sorted(d)}"
    aside = next(path.parent.glob("athlete.json.corrupt-*"))
    assert aside.read_text() == torn
    # the rider re-entering their numbers (setup wizard / settings) does land
    pm.save_athlete({"ftp": 287, "weight_kg": 71.5})
    d = json.loads(path.read_text())
    assert d["ftp"] == 287 and d["lthr"] == 170


def test_control_an_intact_file_loads_and_nothing_is_moved(pm):
    pm.save_athlete({"ftp": 287, "weight_kg": 71.5})
    pm._load_active_profile()
    assert pm.ftp == 287
    assert not list(pm.active_dir.glob("athlete.json.corrupt-*"))


def test_an_empty_file_is_mid_setup_not_corruption(pm):
    path = pm.active_dir / "athlete.json"
    path.write_text("")
    pm._load_active_profile()
    assert not list(path.parent.glob("athlete.json.corrupt-*"))
