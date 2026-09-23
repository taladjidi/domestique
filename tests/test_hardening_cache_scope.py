"""A cached value belongs to the profile it was filled for (contract 5.3).

The audit (2026-09-23, data #5 / rv-facts #2) started the ride-archive fill
(`cached("all_rides")`) for Alice, switched to Bob while it ran, and let it
land: the switch's clear_cache had already run, so Alice's rides were stored
AFTER it and served to Bob for five minutes -- and Alice's CTL with them.

Two guards in cache.py, both cheap:
  * generation: a fill that was running when clear_cache() ran is handed to
    the caller that asked for it but not stored;
  * scope: every entry records the active profile it was filled under
    (registered by profile_manager), and is a miss under any other -- which
    also closes the window between the switch changing the active profile
    and its callbacks clearing the cache.
"""
from __future__ import annotations

import threading
from unittest import mock

import pytest

import cache
import db
import ride_storage as rs
from profile_manager import ProfileManager


def _ride(eid, day, name):
    return {"id": eid, "start_date_local": f"{day}T08:00:00", "type": "Ride",
            "name": name, "moving_time": 3600, "icu_training_load": 60,
            "average_watts": 200, "source": "GARMIN_CONNECT"}


@pytest.fixture
def two_riders(tmp_path):
    p = mock.patch("pathlib.Path.home", return_value=tmp_path)
    p.start()
    old = ProfileManager._instance
    ProfileManager._instance = None
    old_db = db.DB_PATH
    pm = ProfileManager.get()
    a = pm.create_profile("Alice")
    pm.switch(a)
    rs.persist_icu_activity(_ride("iA1", "2026-09-20", "ALICE ride"))
    b = pm.create_profile("Bob")
    pm.switch(b)
    rs.persist_icu_activity(_ride("iB1", "2026-09-21", "BOB ride"))
    pm.switch(a)
    pm._on_switch_callbacks[:] = [cache.clear_cache]   # what app.py registers
    db.shutdown_sync()
    cache.clear_cache()
    yield pm, a, b
    cache.clear_cache()
    p.stop()
    ProfileManager._instance = old
    db.set_db_path(old_db)


def _names():
    return [r.get("name") for r in cache.cached("all_rides", rs.load_all_rides)]


def test_a_fill_in_flight_across_a_switch_is_never_served_to_the_next_rider(two_riders):
    """The audit's reproduction (rv-data p6, MODE=interleave)."""
    pm, a, b = two_riders
    paused, release = threading.Event(), threading.Event()

    def slow():
        out = rs.load_all_rides()
        paused.set()
        release.wait(20)
        return out
    t = threading.Thread(target=lambda: cache.cached("all_rides", slow))
    t.start()
    assert paused.wait(20)
    pm.switch(b)
    release.set()
    t.join(20)
    assert pm.active_id == b
    assert _names() == ["BOB ride"]


def test_a_read_during_the_switch_itself_is_the_new_riders(two_riders):
    """A switch callback that runs BEFORE the cache clear (the sync restart
    starts reading at once) must not get the previous rider's entry."""
    pm, a, b = two_riders
    assert _names() == ["ALICE ride"]
    seen = []
    pm._on_switch_callbacks[:] = [lambda: seen.append(_names()), cache.clear_cache]
    pm.switch(b)
    assert seen == [["BOB ride"]]


def test_a_fill_overtaken_by_clear_cache_is_not_stored(two_riders):
    """clear_cache() after a sync means "the archive changed": a fill that
    read the old archive must not be what the next reader gets for 5 min."""
    calls = []
    paused, release = threading.Event(), threading.Event()

    def slow_old():
        calls.append("old")
        paused.set()
        release.wait(20)
        return ["old archive"]
    t = threading.Thread(target=lambda: cache.cached("k", slow_old))
    t.start()
    assert paused.wait(20)
    cache.clear_cache()
    release.set()
    t.join(20)
    assert cache.cached("k", lambda: calls.append("new") or ["new archive"]) == ["new archive"]


def test_control_same_rider_same_generation_is_served_from_cache(two_riders):
    calls = []
    assert cache.cached("k", lambda: calls.append(1) or [1]) == [1]
    assert cache.cached("k", lambda: calls.append(2) or [2]) == [1]
    assert calls == [1]
