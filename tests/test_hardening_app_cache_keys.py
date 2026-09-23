"""Contract 5.3, the app side: the three caches app.py fills by hand.

cache.cached() now refuses to serve an entry to any profile but the one it
was filled for, and does not store a fill a clear_cache() overtook (Wave 1).
app.py wrote three entries straight into `_cache`, past both guards.
`power_curve_<pid>_…` and `fatigue_resistance_<pid>_…` carry the profile id
in the key; `banister_oos_default` did not: rider A's out-of-sample
validation, still computing when the switch to B ran its clear_cache,
landed afterwards and was served to B for 24 h.
"""
from __future__ import annotations

import threading
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import app as app_module
import cache
import db
from profile_manager import ProfileManager


@pytest.fixture
def two_riders(tmp_path):
    p = mock.patch("pathlib.Path.home", return_value=tmp_path)
    p.start()
    old = ProfileManager._instance
    ProfileManager._instance = None
    old_db = db.DB_PATH
    pm = ProfileManager.get()
    a = pm.create_profile("Alice")
    b = pm.create_profile("Bob")
    pm.switch(a)
    pm._on_switch_callbacks[:] = [cache.clear_cache]   # what app.py registers
    db.shutdown_sync()
    cache.clear_cache()
    yield pm, a, b
    cache.clear_cache()
    p.stop()
    ProfileManager._instance = old
    db.set_db_path(old_db)
    db._sync_stop.clear()


def test_a_banister_fill_in_flight_across_a_switch_is_not_served_to_the_next_rider(two_riders):
    import oos_validation
    pm, a, b = two_riders
    paused, release = threading.Event(), threading.Event()

    def validate(_pid):
        who = pm.active_id
        if who == a:
            paused.set()
            release.wait(20)
        return {"fit_status": "ok", "rider": who}

    client = TestClient(app_module.app)
    with mock.patch.object(oos_validation, "validate_banister_oos", validate):
        t = threading.Thread(target=lambda: client.get("/api/profile/banister-validation"))
        t.start()
        assert paused.wait(20)
        pm.switch(b)
        release.set()
        t.join(20)
        got = client.get("/api/profile/banister-validation").json()
    assert got["rider"] == b


def test_control_the_same_rider_is_served_from_cache(two_riders):
    import oos_validation
    pm, a, b = two_riders
    calls = []
    client = TestClient(app_module.app)
    with mock.patch.object(oos_validation, "validate_banister_oos",
                           lambda _p: calls.append(1) or {"n": len(calls)}):
        assert client.get("/api/profile/banister-validation").json() == {"n": 1}
        assert client.get("/api/profile/banister-validation").json() == {"n": 1}
        assert client.get("/api/profile/banister-validation?refresh=1").json() == {"n": 2}
        assert client.get("/api/profile/banister-validation").json() == {"n": 2}
