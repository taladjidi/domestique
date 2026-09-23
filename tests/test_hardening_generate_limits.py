"""Contract 7.6: /api/plan/generate rejects out-of-range or non-finite
inputs (hours, plan weeks) with 400, using the setup limits.

It accepted hours_per_week -3 / 1e6 / NaN (a 500 or a plan built on it) and
260 plan weeks (audit 2026-09-23, rv-http #8; a port of genprobe.py).
"""
from __future__ import annotations

import json
import math
import types
from datetime import timedelta
from unittest import mock

import pytest

import clock


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    import app as app_module
    import db as db_module
    import training_planner as tp
    from fastapi.testclient import TestClient
    from profile_manager import ProfileManager

    home = tmp_path_factory.mktemp("home")
    patcher = mock.patch("pathlib.Path.home", return_value=home)
    patcher.start()
    saved = (ProfileManager._instance, db_module.DB_PATH, app_module.DATA_DIR,
             app_module.GPX_DIR, tp.PLAN_DIR, tp.WORKOUT_DIR)
    ProfileManager._instance = None
    app_module.DATA_DIR = home / ".domestique"
    try:
        pm = ProfileManager.get()
        pm.switch(pm.create_profile("Rider"))
        db_module.init_db()
        yield types.SimpleNamespace(
            c=TestClient(app_module.app, raise_server_exceptions=False),
            plan=lambda: app_module._plan_dir() / "current_plan.json")
    finally:
        patcher.stop()
        (ProfileManager._instance, old_db, app_module.DATA_DIR,
         app_module.GPX_DIR, tp.PLAN_DIR, tp.WORKOUT_DIR) = saved
        db_module.set_db_path(old_db)
        db_module._sync_stop.clear()
        app_module.clear_cache()


def _body(**over):
    b = {"goal": "general", "hours_per_week": 7.0, "max_weekday": 1.0,
         "max_weekend": 1.5, "plan_weeks": 8, "rest_days": [],
         "daily_availability": {str(i): 1.0 for i in range(7)}}
    b.update(over)
    return b


def _post(env, body):
    # json.dumps emits NaN / Infinity, as a hostile client would
    return env.c.post("/api/plan/generate", content=json.dumps(body),
                      headers={"content-type": "application/json"})


HUGE = 10 ** 400          # an integer literal: json keeps it an int, not inf

BAD = [
    {"hours_per_week": HUGE}, {"plan_weeks": HUGE}, {"daily_availability": {"0": HUGE}},
    {"hours_per_week": -3}, {"hours_per_week": 1e6}, {"hours_per_week": math.nan},
    {"hours_per_week": math.inf}, {"hours_per_week": 0.5}, {"hours_per_week": 41},
    {"plan_weeks": 260}, {"plan_weeks": -1}, {"weeks": 53}, {"plan_weeks": math.nan},
    {"plan_weeks": math.inf}, {"plan_weeks": 1e300},
    {"max_weekday": math.nan}, {"max_weekday": -1}, {"max_weekend": 25},
    {"daily_availability": {"0": math.nan}}, {"daily_availability": {"3": -2}},
    {"daily_availability": {"6": 30}},
]


@pytest.mark.parametrize("over", BAD, ids=[json.dumps(b) for b in BAD])
def test_out_of_range_or_non_finite_is_400_and_writes_nothing(env, over):
    before = env.plan().read_bytes() if env.plan().exists() else None
    r = _post(env, _body(**over))
    assert r.status_code == 400, (over, r.status_code, r.text[:200])
    after = env.plan().read_bytes() if env.plan().exists() else None
    assert after == before


def test_an_event_more_than_52_weeks_away_is_400(env):
    far = (clock.today() + timedelta(weeks=200)).isoformat()
    r = _post(env, _body(goal="event", event_date=far))
    assert r.status_code == 400, r.text[:200]


def test_the_edges_of_the_limits_still_generate(env):
    for over in ({"hours_per_week": 1, "plan_weeks": 4},
                 {"hours_per_week": 40, "plan_weeks": 52}):
        r = _post(env, _body(**over))
        assert r.status_code == 200, (over, r.text[:200])


def test_null_daily_availability_is_the_default_not_a_500(env):
    """Fix-forward F9: `"daily_availability": null` passed the check and then
    `.items()` on None answered 500. Absent and null mean the same."""
    r = _post(env, _body(daily_availability=None))
    assert r.status_code == 200, r.text[:200]


def test_the_week_limit_counts_the_weeks_the_plan_is_built_with(env):
    """Fix-forward F9: the check counted ceil(days / 7) from today; the plan
    is built in whole weeks up to the event, one more near the edge. An
    event 364 days away answered 200 with a 53-week plan; 371 days, 400.
    Now: an accepted plan never exceeds the limit, and past the first
    refusal every later date is refused too."""
    got = []
    for days in (350, 358, 361, 364, 371):
        ev = (clock.today() + timedelta(days=days)).isoformat()
        r = _post(env, _body(goal="event", event_date=ev))
        assert r.status_code in (200, 400), (days, r.status_code, r.text[:200])
        if r.status_code == 200:
            n = len(r.json()["plan_json"]["weeks"])
            assert n <= 52, f"event in {days} days: accepted with {n} weeks"
        got.append(r.status_code)
    assert got[0] == 200 and got[-1] == 400, got
    assert got == sorted(got), f"accepted after a refusal: {got}"


@pytest.mark.parametrize("over", [
    {"rest_days": 5}, {"rest_days": ["x"]}, {"rest_days": [9]},
    {"longest_ride_h_90d": math.nan}, {"longest_ride_h_90d": "abc"},
    {"longest_ride_h_90d": HUGE}, {"longest_ride_h_90d": -1},
], ids=lambda o: json.dumps(o)[:40])
def test_other_malformed_fields_are_400_not_500(env, over):
    """Review of the F9 fix: rest_days and longest_ride_h_90d reached the
    planner unchecked (500s, or a string stored in the goal)."""
    r = _post(env, _body(**over))
    assert r.status_code == 400, (over, r.status_code, r.text[:200])


def test_null_rest_days_is_the_default(env):
    r = _post(env, _body(rest_days=None))
    assert r.status_code == 200, r.text[:200]
