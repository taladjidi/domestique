"""Contract 5.6: per-rider state lives in the active profile's directory.

`readiness_cap_reverted.json`, `last_pr_toast.json` and
`hrv_prompt_state.json` sat at the data root, shared by every rider: Alice
pressing "revert the readiness cap" suppressed Bob's cap that day, Bob's PR
toasts showed on Alice's dashboard, one rider's HRV-prompt dismissal hid it
for all. /api/plan/generate read the root user_prefs.json, which nothing
writes since profiles, instead of the rider's own prefs (audit 2026-09-23,
data #8; rv-data p8_root_state.py; prior S-5).
"""
from __future__ import annotations

import json
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import app as app_module
import cache
import db
import training_planner as tp
from profile_manager import ProfileManager


@pytest.fixture
def two_riders(tmp_path):
    p = mock.patch("pathlib.Path.home", return_value=tmp_path)
    p.start()
    old = ProfileManager._instance
    ProfileManager._instance = None
    saved = (db.DB_PATH, app_module.DATA_DIR, tp.PLAN_DIR, tp.WORKOUT_DIR)
    app_module.DATA_DIR = tmp_path / ".domestique"
    pm = ProfileManager.get()
    a = pm.create_profile("Alice")
    b = pm.create_profile("Bob")
    pm.switch(a)
    pm._on_switch_callbacks[:] = [cache.clear_cache]
    db.shutdown_sync()
    yield pm, a, b
    cache.clear_cache()
    p.stop()
    ProfileManager._instance = old
    old_db, app_module.DATA_DIR, tp.PLAN_DIR, tp.WORKOUT_DIR = saved
    db.set_db_path(old_db)
    db._sync_stop.clear()


def test_one_riders_cap_revert_does_not_suppress_anothers(two_riders):
    pm, a, b = two_riders
    assert not app_module._is_readiness_cap_reverted_today()
    app_module._mark_readiness_cap_reverted_today()          # Alice reverts
    assert app_module._is_readiness_cap_reverted_today()
    pm.switch(b)
    assert not app_module._is_readiness_cap_reverted_today()
    pm.switch(a)
    assert app_module._is_readiness_cap_reverted_today()


def test_pr_toasts_belong_to_the_rider_who_rode(two_riders):
    pm, a, b = two_riders
    app_module._write_pr_toast_queue([{"ride_id": "icu_iA", "n_prs": 1}])
    pm.switch(b)
    assert app_module._read_pr_toast_queue() == []
    pm.switch(a)
    assert app_module._read_pr_toast_queue() == [{"ride_id": "icu_iA", "n_prs": 1}]


def test_an_hrv_prompt_dismissal_is_the_riders_own(two_riders):
    pm, a, b = two_riders
    app_module._write_hrv_prompt_state({"version_dismissed": {}, "permanent_dismissed": True})
    pm.switch(b)
    assert app_module._read_hrv_prompt_state()["permanent_dismissed"] is False


@pytest.mark.parametrize("path_fn", ["_readiness_revert_flag_path",
                                     "_pr_toast_queue_path",
                                     "_hrv_prompt_state_path"])
def test_the_files_live_in_the_active_profile_dir(two_riders, path_fn):
    pm, a, b = two_riders
    assert getattr(app_module, path_fn)().parent == pm.active_dir


def test_generate_reads_the_riders_prefs_not_the_root_file(two_riders):
    """A root user_prefs.json with other rest days must not shape the plan;
    the rider's own saved prefs do."""
    pm, a, b = two_riders
    root = app_module.DATA_DIR / "user_prefs.json"
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text(json.dumps({"rest_days": [0, 1, 2, 3, 4], "hours_per_week": 3}))
    pm.save_prefs({"rest_days": [0], "hours_per_week": 8})
    body = {"goal": "general", "plan_weeks": 4, "max_weekday": 1.5, "max_weekend": 2.0,
            "daily_availability": {str(i): 1.5 for i in range(7)}}
    r = TestClient(app_module.app).post("/api/plan/generate", json=body)
    assert r.status_code == 200, r.text[:300]
    goal = r.json()["plan_json"]["goal"]
    assert goal.get("rest_days") == [0], goal.get("rest_days")
    assert float(goal.get("hours_per_week")) == 8.0
