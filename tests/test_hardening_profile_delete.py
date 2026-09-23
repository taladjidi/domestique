"""Contract 7.1: deleting (or updating) a profile id that is not in the
registry answers 404 and changes nothing.

`delete_profile` decided "this is the last profile" from the registry length
alone, before checking that the id was in it. With one profile left, a
second DELETE of an already-deleted id (a double-click, a retry) or any
unknown id cleared the active pointer: `/` went to the setup wizard, settings
500'd, the plan was served from a stale directory (audit 2026-09-23, rv-http
#1; probe delprobe.py). An unknown id on PUT answered 200 ok.
"""
from __future__ import annotations

import types
from unittest import mock

import pytest


@pytest.fixture()
def stub(tmp_path):
    """Stubbed HOME + fresh ProfileManager (tests/test_account_flow.py)."""
    import app as app_module
    import db as db_module
    import training_planner as tp
    from fastapi.testclient import TestClient
    from profile_manager import ProfileManager

    patcher = mock.patch("pathlib.Path.home", return_value=tmp_path)
    patcher.start()
    saved = (ProfileManager._instance, db_module.DB_PATH, app_module.DATA_DIR,
             app_module.GPX_DIR, tp.PLAN_DIR, tp.WORKOUT_DIR)
    ProfileManager._instance = None
    app_module.DATA_DIR = tmp_path / ".domestique"
    try:
        pm = ProfileManager.get()
        yield types.SimpleNamespace(pm=pm, home=tmp_path,
                                    client=TestClient(app_module.app,
                                                      raise_server_exceptions=False))
    finally:
        patcher.stop()
        (ProfileManager._instance, old_db, app_module.DATA_DIR,
         app_module.GPX_DIR, tp.PLAN_DIR, tp.WORKOUT_DIR) = saved
        db_module.set_db_path(old_db)
        db_module._sync_stop.clear()
        app_module.clear_cache()


def test_a_repeated_delete_leaves_the_last_profile_active(stub):
    c = stub.client
    a = c.post("/api/profiles/create", json={"name": "Anna"}).json()["id"]
    b = c.post("/api/profiles/create", json={"name": "Old"}).json()["id"]
    assert c.post("/api/profiles/switch", json={"id": a}).status_code == 200

    assert c.delete(f"/api/profiles/{b}").status_code == 200
    r = c.delete(f"/api/profiles/{b}")          # the same click, delivered twice
    assert r.status_code == 404, r.text

    listing = c.get("/api/profiles").json()
    assert listing["active"] == a
    assert [p["id"] for p in listing["profiles"]] == [a]
    assert stub.pm.active_id == a
    assert (stub.home / ".domestique" / "profiles" / a).is_dir()


def test_an_unknown_id_is_404_and_touches_no_directory(stub):
    c = stub.client
    a = c.post("/api/profiles/create", json={"name": "Anna"}).json()["id"]
    c.post("/api/profiles/switch", json={"id": a})
    # a directory on disk that the registry does not list is not deletable
    stray = stub.home / ".domestique" / "profiles" / "stray"
    stray.mkdir(parents=True)
    (stray / "keep.txt").write_text("x")
    before = c.get("/api/profiles").json()

    assert c.delete("/api/profiles/stray").status_code == 404
    assert c.delete("/api/profiles/nobody").status_code == 404
    assert (stray / "keep.txt").exists()
    assert c.get("/api/profiles").json() == before


def test_updating_an_unknown_profile_is_404(stub):
    c = stub.client
    a = c.post("/api/profiles/create", json={"name": "Anna"}).json()["id"]
    before = c.get("/api/profiles").json()
    r = c.put("/api/profiles/nobody", json={"name": "X"})
    assert r.status_code == 404, r.text
    assert c.get("/api/profiles").json() == before
    assert c.put(f"/api/profiles/{a}", json={"name": "Anna B"}).status_code == 200
