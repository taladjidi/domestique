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


# ── review findings (2026-09-23) ─────────────────────────────────────────────

def test_a_corrupt_file_is_restored_from_the_last_good_copy(pm):
    """With the last good athlete.json kept beside it, a torn file costs the
    rider nothing: the load restores FTP, weight and test history."""
    path, torn = _torn(pm)
    pm._load_active_profile()
    assert pm.ftp == 287 and pm.weight_kg == 71.5
    assert len(pm.ftp_test_history) == 3
    assert next(path.parent.glob("athlete.json.corrupt-*")).read_text() == torn


def _torn_without_backup(pm):
    path, torn = _torn(pm)
    for b in path.parent.glob("athlete.json.bak"):
        b.unlink()
    pm._load_active_profile()
    return path


def test_with_no_good_copy_a_partial_edit_is_refused_loudly(pm):
    """Refused, not silently dropped: the review found save_athlete({"ftp":
    260}) wrote nothing and update_ftp(265) returned True."""
    path = _torn_without_backup(pm)
    with pytest.raises(ValueError):
        pm.save_athlete({"ftp": 260})
    with pytest.raises(ValueError):
        pm.update_ftp(265)
    assert not path.exists()


def test_a_refused_write_leaves_the_profile_in_memory_unchanged(pm):
    """Fix-forward F4 (the evaluator's p3b_lockout): the refusal came from
    the write, after save_athlete had already done `_athlete.update(data)`.
    POST /api/settings {"ftp": 260} answered 400 while GET /api/settings
    showed 260, and a refused record_ftp_test stayed in memory and was
    written by the next accepted save."""
    from fastapi.testclient import TestClient
    import app as app_module
    path = _torn_without_backup(pm)
    before = json.loads(json.dumps(pm._athlete))
    c = TestClient(app_module.app, base_url="http://127.0.0.1")
    assert c.post("/api/settings", json={"ftp": 260}).status_code == 400
    got = c.get("/api/settings")
    assert got.status_code == 200 and got.json().get("ftp") != 260, got.json().get("ftp")
    with pytest.raises(ValueError):
        pm.record_ftp_test("ramp", 270, source="refused", applied=True)
    with pytest.raises(ValueError):
        pm.update_ftp(265)
    with pytest.raises(ValueError):
        pm._set_fact("wprime", 20000, "icu")      # the sync thread's mirror
    assert pm._athlete == before
    pm.save_athlete({"ftp": 287, "weight_kg": 71.5})     # accepted
    d = json.loads(path.read_text())
    assert d["ftp"] == 287 and d.get("wprime_j") != 20000
    assert not [t for t in d.get("ftp_test_history", []) if t.get("source") == "refused"], \
        "a refused FTP test was persisted by the next accepted save"


def test_the_guard_survives_a_restart(pm):
    """After a restart the torn file is absent (moved aside), not corrupt; the
    aside copy is what says a partial write must not become the file."""
    path = _torn_without_backup(pm)
    ProfileManager._instance = None
    fresh = ProfileManager.get()
    with pytest.raises(ValueError):
        fresh.save_athlete({"lthr": 170, "lthr_source": "icu"})
    assert not path.exists()
    fresh.save_athlete({"ftp": 287, "weight_kg": 71.5})
    assert json.loads(path.read_text())["ftp"] == 287


def test_a_profile_file_respects_the_umask(pm):
    """Review finding: fchmod(0o644) ignored the umask that os.open honoured,
    so under umask 077 athlete.json became world-readable. .env stays 0600."""
    import os
    import stat
    old = os.umask(0o077)
    try:
        pm.save_athlete({"ftp": 250, "weight_kg": 70})
        pm.save_env("i1", "k")
    finally:
        os.umask(old)
    mode = lambda p: stat.S_IMODE(p.stat().st_mode)   # noqa: E731
    assert mode(pm.active_dir / "athlete.json") == 0o600
    assert mode(pm.active_dir / ".env") == 0o600
