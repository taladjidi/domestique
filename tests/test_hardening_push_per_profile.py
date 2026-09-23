"""Contract 5.7: the calendar push's daily-reconcile gate and its last-result
status are per profile.

`_icu_push_last_daily` was one module-wide date: rider A's daily reconcile
latched the day, and after a switch rider B's calendar was not reconciled
until tomorrow. `_icu_push_last_result` was one module-wide dict: B's
status showed A's push counts and errors (audit 2026-09-23, integ #7,
struct #6; rv-integ test_p8_daily.py).
"""
from __future__ import annotations

from unittest import mock

import pytest
from fastapi.testclient import TestClient

import app as app_module
import cache
import db
import icu_calendar_push as icp
import training_planner as tp
from profile_manager import ProfileManager


@pytest.fixture
def two_riders(tmp_path, monkeypatch):
    p = mock.patch("pathlib.Path.home", return_value=tmp_path)
    p.start()
    old = ProfileManager._instance
    ProfileManager._instance = None
    saved = (db.DB_PATH, app_module.DATA_DIR, tp.PLAN_DIR, tp.WORKOUT_DIR)
    app_module.DATA_DIR = tmp_path / ".domestique"
    pm = ProfileManager.get()
    a = pm.create_profile("Alice")
    b = pm.create_profile("Bob")
    for pid in (a, b):
        pm.switch(pid)
        pm.save_prefs({"icu_calendar_sync": True})
    pm.switch(a)
    pm._on_switch_callbacks[:] = [cache.clear_cache]
    db.shutdown_sync()
    monkeypatch.setattr(app_module, "_icu_push_last_daily", {})
    monkeypatch.setattr(app_module, "_icu_push_last_result", {})
    yield pm, a, b
    cache.clear_cache()
    p.stop()
    ProfileManager._instance = old
    old_db, app_module.DATA_DIR, tp.PLAN_DIR, tp.WORKOUT_DIR = saved
    db.set_db_path(old_db)
    db._sync_stop.clear()


def test_the_daily_reconcile_runs_for_each_rider(two_riders):
    pm, a, b = two_riders
    ran = []
    with mock.patch.object(icp, "reconcile", lambda: ran.append(pm.active_id) or {"pushed": 0}):
        app_module._icu_push_daily_from_sync()
        app_module._icu_push_daily_from_sync()      # same rider, same day: latched
        pm.switch(b)
        app_module._icu_push_daily_from_sync()
        pm.switch(a)
        app_module._icu_push_daily_from_sync()      # A is still latched today
    assert ran == [a, b]


def test_the_push_status_is_the_active_riders(two_riders):
    pm, a, b = two_riders
    c = TestClient(app_module.app)
    with mock.patch.object(icp, "reconcile", lambda: {"pushed": 3, "error": None}):
        app_module._icu_push_daily_from_sync()
    assert c.get("/api/icu/push").json()["last_result"]["pushed"] == 3
    pm.switch(b)
    assert c.get("/api/icu/push").json()["last_result"] is None
    pm.switch(a)
    assert c.get("/api/icu/push").json()["last_result"]["pushed"] == 3
