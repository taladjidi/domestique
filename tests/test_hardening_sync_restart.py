"""Switching profile while a sync pass is stuck in a slow fetch leaves syncing
working for the new profile (contract 5.4).

The audit (2026-09-23, integ #1; prior STA-7) held the background pass inside
a slow intervals.icu fetch and switched profile. restart_sync joined the old
thread for 5 s, found it still alive, and REFUSED to start a new one -- with
the shared stop flag left set. From then on there was no sync thread, every
sync_write_gate raised SyncAborted (the lazy ride sync, "Sync now"), and
/api/sync/status still read healthy.

Now each sync thread owns its stop event. A superseded thread keeps its own
(set) flag, so it exits at its next check and every write it attempts
aborts; the new profile gets a fresh thread and a fresh flag at once. The
status says whether the loop is alive.
"""
from __future__ import annotations

import threading
import time
from unittest import mock

import pytest

import db
from profile_manager import ProfileManager

SLOW = 6.5      # longer than restart_sync's 5 s join: the audit's case


@pytest.fixture
def two_profiles(tmp_path):
    p = mock.patch("pathlib.Path.home", return_value=tmp_path)
    p.start()
    old = ProfileManager._instance
    ProfileManager._instance = None
    old_db = db.DB_PATH
    pm = ProfileManager.get()
    a, b = pm.create_profile("Alpha"), pm.create_profile("Beta")
    for pid in (b, a):
        pm.switch(pid)
        pm.save_env(f"i-{pid}", "key")
    pm._on_switch_callbacks[:] = [db.restart_sync]      # what app.py registers
    db.shutdown_sync()
    yield pm, a, b
    db.shutdown_sync(timeout=SLOW + 5)
    p.stop()
    ProfileManager._instance = old
    db.set_db_path(old_db)
    db.close_all_connections()


def _rows(pm, pid):
    import sqlite3
    conn = sqlite3.connect(str(pm._profiles_dir / pid / "health_tracker.db"))
    try:
        return [r[0] for r in conn.execute("SELECT date FROM wellness ORDER BY date")]
    finally:
        conn.close()


def test_a_switch_during_a_slow_fetch_leaves_sync_working(two_profiles):
    pm, a, b = two_profiles
    entered, calls = threading.Event(), []

    def fetch_wellness(days=90):
        calls.append(pm.active_id)
        if len(calls) == 1:                 # the old profile's pass: stuck
            entered.set()
            time.sleep(SLOW)
            return [{"id": "2026-09-01", "ctl": 1.0, "atl": 1.0, "sportInfo": []}]
        return [{"id": "2026-09-02", "ctl": 2.0, "atl": 2.0, "sportInfo": []}]

    with mock.patch.object(db, "fetch_wellness", fetch_wellness), \
         mock.patch.object(db, "fetch_activities", lambda days=90: []):
        db.start_background_sync()
        assert entered.wait(5)
        old_thread = db._sync_thread
        pm.switch(b)                        # on_switch -> db.restart_sync
        # the new profile's loop is running now, not after the stuck pass
        assert db._sync_thread is not old_thread
        assert db.get_sync_status()["loop_alive"] is True
        with db.sync_write_gate(db.snapshot_sync_identity()):
            pass                            # other writers are not blocked
        deadline = time.time() + 5
        while "2026-09-02" not in _rows(pm, b) and time.time() < deadline:
            time.sleep(0.05)
        assert _rows(pm, b) == ["2026-09-02"], "the new profile's pass did not write"
        old_thread.join(SLOW + 5)
        assert not old_thread.is_alive(), "the superseded thread must exit"
    assert _rows(pm, a) == [], "the superseded pass wrote after the switch"
    assert db.get_sync_status()["loop_alive"] is True


def test_the_status_reports_a_dead_loop(two_profiles):
    db.shutdown_sync()
    assert db.get_sync_status()["loop_alive"] is False


def test_a_superseded_threads_failure_does_not_count_against_the_new_one(two_profiles):
    """The stuck pass finally fails (network) after the switch: that is the
    old thread's news, not a strike against the new profile's sync."""
    pm, a, b = two_profiles
    entered = threading.Event()
    first = []

    def fetch_wellness(days=90):
        if not first:
            first.append(1)
            entered.set()
            time.sleep(SLOW)
            raise OSError("network unreachable")
        return []

    with mock.patch.object(db, "fetch_wellness", fetch_wellness), \
         mock.patch.object(db, "fetch_activities", lambda days=90: []):
        db.start_background_sync()
        assert entered.wait(5)
        old_thread = db._sync_thread
        pm.switch(b)
        old_thread.join(SLOW + 5)
        st = db.get_sync_status()
    assert st["consecutive_failures"] == 0 and st["last_error"] is None, st


def test_a_restart_without_a_switch_silences_the_stuck_pass(two_profiles):
    """POST /api/sync (restart_sync on the SAME profile): the snapshot matches, so
    only the old thread's own stop flag keeps its late write out."""
    pm, a, b = two_profiles
    entered, calls = threading.Event(), []

    def fetch_wellness(days=90):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            time.sleep(SLOW)
            return [{"id": "2026-09-01", "ctl": 1.0, "atl": 1.0, "sportInfo": []}]
        return [{"id": "2026-09-02", "ctl": 2.0, "atl": 2.0, "sportInfo": []}]

    with mock.patch.object(db, "fetch_wellness", fetch_wellness), \
         mock.patch.object(db, "fetch_activities", lambda days=90: []):
        db.start_background_sync()
        assert entered.wait(5)
        old_thread = db._sync_thread
        db.restart_sync()
        old_thread.join(SLOW + 5)
    assert _rows(pm, a) == ["2026-09-02"], "the superseded pass wrote late"
