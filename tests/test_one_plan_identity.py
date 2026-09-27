"""Contract 10.3: the plan's identity is the profile, not a global.

The audit's D8 (d190fa82): _maybe_auto_reforecast for rider A read its
inputs (the activities) after a profile switch to rider B had landed, and
wrote A's plan with B's activities. Now a request or job captures its
profile once (plan_store.capture) and every store call names it; a switch
spends the captures, and a write whose capture is spent writes nothing.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import app as app_module
import clock
import db as db_mod
import plan_store
import profile_manager as pm_mod
import training_planner as tp
from test_hardening_plan_store import _week


@pytest.fixture
def two_riders(tmp_path, monkeypatch):
    saved_env = {k: os.environ.get(k) for k in ("ICU_ATHLETE_ID", "ICU_API_KEY", "ICU_ACCESS_TOKEN")}
    orig_db, orig_wd = db_mod.DB_PATH, tp.WORKOUT_DIR
    pm_mod.ProfileManager._instance = None
    with patch("pathlib.Path.home", return_value=Path(tmp_path)):
        pm = pm_mod.ProfileManager.get()
    a, b = pm.create_profile("Alice"), pm.create_profile("Bob")
    pm.switch(a)
    monkeypatch.setattr(app_module, "get_today_metrics", lambda *x, **k: {})
    today = clock.today()
    mon = today - timedelta(days=today.weekday())
    plan = {"goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0, 4, 6],
                     "target_date": (mon + timedelta(weeks=8)).isoformat()},
            "phases": [], "weeks": [_week(mon + timedelta(weeks=k), k + 1) for k in range(8)],
            "generated": (clock.now() - timedelta(days=3)).isoformat()}
    plan_store.update(a, lambda _p: plan, reason="test seed")
    yield pm, a, b
    db_mod.shutdown_sync()
    db_mod.close_all_connections()
    db_mod.set_db_path(orig_db)
    tp.WORKOUT_DIR = orig_wd
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    pm_mod.ProfileManager._instance = None


def test_the_control_adapts_riders_plan(two_riders):
    """Without a switch the adaptation does write A's plan (the daily
    reconcile stamp at least): the test below is not vacuous."""
    pm, a, b = two_riders
    before = plan_store.version(a)
    app_module._maybe_auto_reforecast(app_module._pid(), 1)
    assert plan_store.version(a) != before


def test_a_switch_mid_adaptation_writes_nothing(two_riders, monkeypatch):
    pm, a, b = two_riders
    pid = app_module._pid()
    assert pid == a
    before = plan_store._paths(a).live.read_bytes()
    real = db_mod.query_activities
    seen = {}

    def q_and_switch(*x, **k):
        if not seen:
            seen["switched"] = True
            pm.switch(b)                   # rider B's activities from here on
        return real(*x, **k)
    monkeypatch.setattr(db_mod, "query_activities", q_and_switch)
    app_module._maybe_auto_reforecast(pid, 1)
    assert seen, "the switch never landed (test is vacuous)"
    assert plan_store._paths(a).live.read_bytes() == before, "A's plan written after the switch"
    assert plan_store.read(b) is None


def test_a_capture_is_spent_by_a_switch_and_refuses(two_riders):
    pm, a, b = two_riders
    pid = app_module._pid()
    pm.switch(b)
    with pytest.raises(plan_store.ProfileSwitched):
        plan_store.update(pid, lambda p: {**p, "x": 1}, reason="test")
    assert "x" not in plan_store.read(a)


def test_each_profile_reads_its_own_plan_whoever_is_active(two_riders):
    pm, a, b = two_riders
    pm.switch(b)
    plan_store.update(app_module._pid(), lambda _p: {"weeks": [], "who": "b"}, reason="test")
    assert plan_store.read(a)["goal"]["type"] == "general"
    assert plan_store.read(b)["who"] == "b"
    assert plan_store._paths(a).dir != plan_store._paths(b).dir
