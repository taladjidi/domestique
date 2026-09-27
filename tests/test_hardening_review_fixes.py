"""Regressions from the adversarial review of the Wave-2 hardening commits
(2026-09-23). Each test was a probe that failed on 89b31fe2.

1. auto-recalc's "fresh" answer went through the rebuild's inputs, now
   hoisted out of the plan lock: a metrics failure turned it into a 500 and
   every Plan-tab open fetched wellness (3.3.2 Lapo #2 says it must not).
2. async /api/ride/import still waited for the plan lock on the event loop
   (_maybe_auto_reforecast -> tp.update_plan): contract 4.2.
3. generate could not repair a current_plan.json that is not valid UTF-8,
   and echoed the codec error in a 400.
4. a non-object body to /api/ride/{id}/rpe was read as {} = {"rpe": null}
   and CLEARED the rider's rating (worse since contract 7.5).
5. the Host check was not the outermost middleware: a refused Host still ran
   the plan heal of a POST under /api/profiles.
6. generate stored "8" / true as hours, 500'd on {"weeks": null}, accepted a
   weekday "9".
7. Host "[::1]evil" was read as "[::1]".
"""
from __future__ import annotations

import json
import plan_seam  # noqa: E402  (the plan dir of the active profile)
import threading
import time
import types
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import clock
import plan_store
import training_planner as tp
from test_hardening_plan_store import _week
from test_hardening_ride_writers_app import EXT, ride  # noqa: F401 (fixture)


def _read(p):
    return json.loads(Path(p).read_text())


@pytest.fixture
def fresh_plan(tmp_path, monkeypatch):
    import app as A
    today = clock.today()
    mon = today - timedelta(days=today.weekday())
    fresh = (clock.now() - timedelta(days=2)).isoformat()
    plan = {"goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0, 4, 6],
                     "target_date": (mon + timedelta(weeks=8)).isoformat()},
            "phases": [], "weeks": [_week(mon + timedelta(weeks=k), k + 1) for k in range(8)],
            "generated": fresh, "recalc_date": fresh}
    plan_seam.bind_active(tmp_path)
    jp = tmp_path / "current_plan.json"
    jp.write_text(json.dumps(plan))
    A.clear_cache()
    yield A, jp, TestClient(A.app, base_url="http://127.0.0.1", raise_server_exceptions=False), mon
    A.clear_cache()


# ── 1 ──
def test_a_fresh_auto_recalc_survives_a_metrics_failure(fresh_plan, monkeypatch):
    A, jp, c, _ = fresh_plan
    wellness = []

    def boom(*a, **k):
        raise RuntimeError("intervals.icu down")
    monkeypatch.setattr(A, "get_today_metrics", boom)
    monkeypatch.setattr(A, "_planning_ctl", boom)
    monkeypatch.setattr(A, "fetch_wellness", lambda *a, **k: wellness.append(1) or [])
    before = jp.read_bytes()
    r = c.post("/api/plan/auto-recalc")
    assert r.status_code == 200 and r.json().get("action") == "fresh", r.text[:300]
    assert wellness == [], "a fresh plan needs no wellness fetch"
    assert jp.read_bytes() == before


# ── 2 ──
def test_no_async_handler_reaches_the_plan_lock_through_a_sync_helper():
    """The call-graph half of the 4.2 invariant (test_planner_fixes checks
    the direct calls): no coroutine calls, directly, a module-level sync
    function that reaches the plan store (plan_store.update or one of its
    named operations; tp.update_plan / atomic_write_plan / plan_write_lock
    before the one-plan round). lifespan runs before the server serves."""
    import ast
    import app as A
    tree = ast.parse(Path(A.__file__).read_text(encoding="utf-8"))
    fns = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def calls(fn):
        names, store = set(), False
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                        and f.value.id == "plan_store"
                        and f.attr in ("update", "restore_from_backup", "adopt_legacy_root",
                                       "copy_legacy_plans", "drop")):
                    store = True
                if isinstance(f, ast.Name):
                    names.add(f.id)
        return names, store
    info = {k: calls(v) for k, v in fns.items()}
    sync = {k for k, v in fns.items() if isinstance(v, ast.FunctionDef)}
    blocking = {k for k in sync if info[k][1]}
    grew = True
    while grew:
        grew = False
        for k in sync - blocking:
            if info[k][0] & blocking:
                blocking.add(k)
                grew = True
    offenders = []
    for name, fn in fns.items():
        if not isinstance(fn, ast.AsyncFunctionDef) or name == "lifespan":
            continue
        stack = list(fn.body)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in blocking:
                offenders.append(f"{name}:{n.lineno} -> {n.func.id}")
            stack.extend(ast.iter_child_nodes(n))
    assert offenders == []
    assert "_maybe_auto_reforecast" in blocking      # the scan sees the helper


def test_a_ride_import_does_not_stall_the_event_loop(fresh_plan, monkeypatch):
    import anyio.from_thread
    A, jp, c, _ = fresh_plan
    monkeypatch.setattr(A, "get_today_metrics", lambda *a, **k: {})
    res = {}
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        held, done = threading.Event(), threading.Event()

        def slow_writer():
            with plan_store._LOCK:
                held.set()
                done.wait(3)
        tw = threading.Thread(target=slow_writer)
        tw.start()
        assert held.wait(5)
        tm = threading.Thread(target=lambda: res.setdefault("imp", c.post(
            "/api/ride/import",
            files={"fit_file": ("x.fit", b"\x0e" * 200, "application/octet-stream")})))
        tm.start()
        time.sleep(0.5)
        t0 = time.monotonic()
        c.get("/api/version")
        elapsed = time.monotonic() - t0
        done.set()
        tw.join(10)
        tm.join(30)
        c.portal = None
    assert elapsed < 0.5, f"/api/version took {elapsed:.2f}s behind the plan lock"


# ── 3 ──
@pytest.fixture
def rider_env(tmp_path):
    import app as app_module
    import db as db_module
    from profile_manager import ProfileManager
    patcher = mock.patch("pathlib.Path.home", return_value=tmp_path)
    patcher.start()
    saved = (ProfileManager._instance, db_module.DB_PATH, app_module.DATA_DIR,
             app_module.GPX_DIR, tp.WORKOUT_DIR)
    ProfileManager._instance = None
    app_module.DATA_DIR = tmp_path / ".domestique"
    try:
        pm = ProfileManager.get()
        pm.switch(pm.create_profile("Rider"))
        db_module.init_db()
        yield types.SimpleNamespace(
            c=TestClient(app_module.app, raise_server_exceptions=False),
            plan=lambda: plan_seam.active_plan_dir() / "current_plan.json")
    finally:
        patcher.stop()
        (ProfileManager._instance, old_db, app_module.DATA_DIR,
         app_module.GPX_DIR, tp.WORKOUT_DIR) = saved
        db_module.set_db_path(old_db)
        db_module._sync_stop.clear()
        app_module.clear_cache()


def _gen(env, **over):
    b = {"goal": "general", "hours_per_week": 7.0, "max_weekday": 1.0,
         "max_weekend": 1.5, "plan_weeks": 8, "rest_days": [],
         "daily_availability": {str(i): 1.0 for i in range(7)}}
    b.update(over)
    return env.c.post("/api/plan/generate", content=json.dumps(b),
                      headers={"content-type": "application/json"})


@pytest.mark.parametrize("corrupt", [b'{"goal": "caf\xc3', b'{"weeks": ['],
                         ids=["utf8-truncated", "json-truncated"])
def test_generate_repairs_an_unreadable_plan(rider_env, corrupt):
    p = rider_env.plan()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(corrupt)
    r = _gen(rider_env)
    assert r.status_code == 200, r.text[:300]
    assert isinstance(_read(p), dict)


# ── 4 ──
@pytest.mark.parametrize("body", [b"[]", b'"x"', b"null", b"5", b"{not json"])
def test_an_unreadable_rpe_body_is_400_and_keeps_the_rating(ride, body):  # noqa: F811
    import app as A
    c = TestClient(A.app, raise_server_exceptions=False)
    assert c.post(f"/api/ride/icu_{EXT}/rpe", json={"rpe": 7}).status_code == 200
    r = c.post(f"/api/ride/icu_{EXT}/rpe", content=body,
               headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert _read(ride).get("rpe") == 7
    # clearing is explicit, and still works
    assert c.post(f"/api/ride/icu_{EXT}/rpe", json={"rpe": None}).status_code == 200
    assert "rpe" not in _read(ride)


# ── 5 ──
def test_a_refused_host_reaches_no_other_middleware(monkeypatch):
    import app as A
    calls = []
    monkeypatch.setattr(A, "_heal_plan_on_disk", lambda pid: calls.append(1))
    monkeypatch.setenv("DOMESTIQUE_ALLOWED_HOSTS", "testserver")
    c = TestClient(A.app, raise_server_exceptions=False)
    r = c.post("/api/profiles/switch", json={"id": "x"},
               headers={"host": "rebind.attacker.example"})
    assert r.status_code == 400
    assert calls == []


# ── 6 ──
@pytest.mark.parametrize("over", [
    {"hours_per_week": "8"}, {"hours_per_week": True}, {"hours_per_week": None},
    {"max_weekday": "1.5"}, {"plan_weeks": "8"}, {"plan_weeks": True},
    {"plan_weeks": 2.7}, {"weeks": None}, {"daily_availability": {"0": "2"}},
    {"daily_availability": {"9": 1.0}}, {"daily_availability": {"x": 1.0}},
    {"hours_per_week": [8]},
], ids=lambda o: json.dumps(o))
def test_generate_refuses_values_that_are_not_numbers(rider_env, over):
    p = rider_env.plan()
    before = p.read_bytes() if p.exists() else None
    r = _gen(rider_env, **over)
    assert r.status_code == 400, (over, r.status_code, r.text[:200])
    assert (p.read_bytes() if p.exists() else None) == before


# ── 7 ──
@pytest.mark.parametrize("host", ["[::1]evil", "[::1]evil:80", "[::1"])
def test_nothing_but_a_port_may_follow_an_ipv6_literal(monkeypatch, host):
    import app as A
    monkeypatch.setenv("DOMESTIQUE_ALLOWED_HOSTS", "testserver")
    c = TestClient(A.app, raise_server_exceptions=False)
    assert c.get("/api/version", headers={"host": host}).status_code == 400
    assert c.get("/api/version", headers={"host": "[::1]:22400"}).status_code == 200
