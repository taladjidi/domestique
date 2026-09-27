"""Contract 7.5: a POST whose JSON body is not an object answers 400, not
500, on every route that reads a body.

`_get_json_body` returned any JSON value; 21 POST routes then called
`.get` on a list / string / None and answered 500 (audit 2026-09-23, rv-http
#7; a port of the reviewer's postfuzz.py). The `body: dict` routes answered
FastAPI's 422.
"""
from __future__ import annotations

import types
import plan_seam  # noqa: E402  (the plan dir of the active profile)
from unittest import mock

import pytest

NON_OBJECTS = {"list": b"[]", "str": b'"x"', "null": b"null", "number": b"5"}

# routes whose side effects outlive a request (threads, purges, a switch)
SKIP = {"/api/profiles/{profile_id}", "/api/profiles/switch",
        "/api/profile/backfill-history", "/api/profile/dfa-backfill",
        "/api/wellness/backfill-3d-fitness", "/api/sync", "/api/rides/sync",
        "/api/setup/save", "/api/profiles/create", "/api/icu/disconnect"}
FILL = {"profile_id": "nobody", "activity_id": "i1", "ride_id": "r1"}

# Every route that reads a JSON body (the 21 the fuzz found 500ing, plus the
# `body: dict` routes that answered 422).
BODY_READERS = {
    "/api/daily-log", "/api/metrics/log", "/api/plan/accept-redraw",
    "/api/plan/add-race", "/api/plan/auto-adjust", "/api/plan/dismiss-session",
    "/api/plan/ftp-test-type", "/api/plan/generate", "/api/plan/mark-unavailable",
    "/api/plan/move-session", "/api/plan/preview-redraw", "/api/plan/re-draw",
    "/api/plan/save-availability", "/api/plan/swap-type",
    "/api/readiness/apply-tier-down", "/api/ride/{ride_id}/ftp-test-review",
    "/api/routes/suggest", "/api/today-session/persist", "/api/wellness/manual-hrv",
    "/api/setup/test-icu", "/api/setup/check-activities", "/api/settings",
    "/api/profile/update-ftp", "/api/settings/zones",
    "/api/activity/{activity_id}/race",
}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import app as app_module
    import db as db_module
    import training_planner as tp
    from fastapi.testclient import TestClient
    from profile_manager import ProfileManager

    home = tmp_path_factory.mktemp("home")
    patcher = mock.patch("pathlib.Path.home", return_value=home)
    patcher.start()
    saved = (ProfileManager._instance, db_module.DB_PATH, app_module.DATA_DIR,
             app_module.GPX_DIR, tp.WORKOUT_DIR)
    ProfileManager._instance = None
    app_module.DATA_DIR = home / ".domestique"
    try:
        pm = ProfileManager.get()
        pm.switch(pm.create_profile("Rider"))
        db_module.init_db()
        c = TestClient(app_module.app, raise_server_exceptions=False)
        c.post("/api/plan/generate", json={
            "goal": "general", "hours_per_week": 7.0, "max_weekday": 1.0,
            "max_weekend": 1.5, "plan_weeks": 4, "rest_days": [],
            "daily_availability": {str(i): 1.0 for i in range(7)}})
        yield types.SimpleNamespace(c=c, app=app_module)
    finally:
        patcher.stop()
        (ProfileManager._instance, old_db, app_module.DATA_DIR,
         app_module.GPX_DIR, tp.WORKOUT_DIR) = saved
        db_module.set_db_path(old_db)
        db_module._sync_stop.clear()
        app_module.clear_cache()


def _routes(app_module):
    out = []
    for r in app_module.app.routes:
        for m in getattr(r, "methods", ()) or ():
            if m in ("POST", "PUT") and r.path not in SKIP:
                out.append((m, r.path))
    return sorted(set(out))


def _url(path):
    for k, v in FILL.items():
        path = path.replace("{%s}" % k, v)
    return path


def test_no_route_answers_5xx_to_a_non_object_body(client):
    bad = {}
    for m, path in _routes(client.app):
        for label, raw in NON_OBJECTS.items():
            r = client.c.request(m, _url(path), content=raw,
                                 headers={"content-type": "application/json"})
            if r.status_code >= 500:
                bad.setdefault(f"{m} {path}", []).append(label)
    assert bad == {}


def test_every_body_reader_answers_400(client):
    routes = {p: m for m, p in _routes(client.app)}
    missing = BODY_READERS - set(routes)
    assert not missing, f"routes renamed? {missing}"
    wrong = {}
    for path in sorted(BODY_READERS):
        for label, raw in NON_OBJECTS.items():
            r = client.c.request(routes[path], _url(path), content=raw,
                                 headers={"content-type": "application/json"})
            if r.status_code != 400:
                wrong.setdefault(path, []).append((label, r.status_code))
    assert wrong == {}
