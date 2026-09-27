"""Contract 5.1: one owner of the active workout directory.

The app and the planner each kept a WORKOUT_DIR. /api/setup/save rebound the
app's alone, so the planner went on planning from the old library while the
app served (and the calendar push read) the new one: every planned session's
download answered 404 and the push skipped them all as `file_missing` until
a restart (audit 2026-09-23, rv-http #2 / rv-struct #1: 40/40 files 404,
calendar push `file_missing: 12`).

The planner's WORKOUT_DIR is the owner; `tp.active_workout_dir()` is how
everything else reads it. The calendar push reads the app's accessor, which
Wave 2 points at the planner's (notes/hardening-interfaces-planio.md). The
end-to-end test through /api/setup/save is strict-xfail until then.
"""
from __future__ import annotations

import shutil
import plan_seam  # noqa: E402  (the plan dir of the active profile)
import types
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest

import clock
import training_planner as tp

BUNDLED = Path(tp.__file__).parent / "workouts"


def _some_bundled_zwo() -> Path:
    return next(p for p in sorted(BUNDLED.glob("z2_*.zwo")))


def test_the_planner_accessor_reads_late(monkeypatch, tmp_path):
    captured = tp.active_workout_dir()
    monkeypatch.setattr(tp, "WORKOUT_DIR", tmp_path)
    assert tp.active_workout_dir() == tmp_path
    assert captured != tmp_path


def test_calendar_push_reads_the_active_workout_dir_accessor(monkeypatch, tmp_path):
    """The push used `app.WORKOUT_DIR` directly: a second read path that
    would stay behind when the accessor moves to the planner's directory."""
    import app
    import icu_calendar_push as icp
    from profile_manager import ProfileManager

    active = tmp_path / "active"
    stale = tmp_path / "stale"
    active.mkdir()
    stale.mkdir()
    shutil.copy(_some_bundled_zwo(), active / "ONLY_ACTIVE.zwo")
    monkeypatch.setattr(app, "WORKOUT_DIR", stale, raising=False)
    monkeypatch.setattr(app, "active_workout_dir", lambda: active)

    day = (clock.today() + timedelta(days=1)).isoformat()
    plan = {"weeks": [{"week_num": 1, "start": clock.today().isoformat(),
                       "end": (clock.today() + timedelta(days=6)).isoformat(),
                       "sessions": [{"day": day, "session_type": "z2",
                                     "duration_min": 60, "zwo_file": "ONLY_ACTIVE.zwo",
                                     "zwo_name": "Only active"}]}]}
    pm = ProfileManager.get()
    events, skipped, _broken = icp._desired_events(pm, plan, clock.today(), 14, pm.active_id)
    assert [s["reason"] for s in skipped] == []
    assert [e["filename"] for e in events] == ["ONLY_ACTIVE.zwo"]


# ── end to end through /api/setup/save (Wave 2) ──────────────────────────────

@pytest.fixture()
def stub(tmp_path):
    """Stubbed HOME + fresh ProfileManager, restored after (the
    tests/test_account_flow.py pattern)."""
    import app as app_module
    import db as db_module
    from fastapi.testclient import TestClient
    from profile_manager import ProfileManager

    patcher = mock.patch("pathlib.Path.home", return_value=tmp_path)
    patcher.start()
    # getattr: Wave 2 may retire the app's own WORKOUT_DIR global.
    saved = (ProfileManager._instance, db_module.DB_PATH, app_module.DATA_DIR,
             getattr(app_module, "WORKOUT_DIR", None), app_module.GPX_DIR, tp.WORKOUT_DIR)
    ProfileManager._instance = None
    app_module.DATA_DIR = tmp_path / ".domestique"
    try:
        pm = ProfileManager.get()
        pid = pm.create_profile("Rider")
        pm.switch(pid)
        yield types.SimpleNamespace(home=tmp_path, pm=pm, pid=pid, app=app_module,
                                    client=TestClient(app_module.app,
                                                      base_url="http://127.0.0.1"))
    finally:
        patcher.stop()
        (ProfileManager._instance, old_db, app_module.DATA_DIR, old_app_wd,
         app_module.GPX_DIR, tp.WORKOUT_DIR) = saved
        if old_app_wd is not None:
            app_module.WORKOUT_DIR = old_app_wd
        db_module.set_db_path(old_db)
        db_module._sync_stop.clear()
        app_module.clear_cache()


def test_setup_save_moves_the_planner_and_the_app_together(stub):
    import icu_calendar_push as icp
    custom = stub.home / "mylib"
    custom.mkdir()
    src = _some_bundled_zwo()
    for i in range(3):
        shutil.copy(src, custom / f"ONLY_CUSTOM_{i}.zwo")

    r = stub.client.post("/api/setup/save", json={"workout_dir": str(custom)})
    assert r.status_code == 200, r.text

    # one directory
    assert Path(stub.app.active_workout_dir()) == Path(tp.active_workout_dir()) == custom
    # the planner plans from it
    files = {row.get("File") for row in tp.load_workout_library()}
    assert "ONLY_CUSTOM_0.zwo" in files
    # a file the planner would plan is the file the app serves
    planned = sorted(f for f in files if f)[0]
    r = stub.client.get(f"/api/workout/download/{planned}")
    assert r.status_code == 200, (planned, r.status_code, r.text[:120])
    # and the calendar push finds it
    day = (clock.today() + timedelta(days=1)).isoformat()
    plan = {"weeks": [{"week_num": 1, "start": clock.today().isoformat(),
                       "end": (clock.today() + timedelta(days=6)).isoformat(),
                       "sessions": [{"day": day, "session_type": "z2", "duration_min": 60,
                                     "zwo_file": planned, "zwo_name": "x"}]}]}
    _ev, skipped, _b = icp._desired_events(stub.pm, plan, clock.today(), 14, stub.pid)
    assert "file_missing" not in [s["reason"] for s in skipped]
