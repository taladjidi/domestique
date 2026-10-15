"""3.13.0 pre-release review: the findings fixed before the release.

H1  an async plan endpoint waiting for the plan lock froze every request
M1  the weekly auto-recalc held the plan lock across intervals.icu reads
M2  a reconcile queued before "sync off" pushed everything back afterwards
M3  a swap from a missed past day put the other workout on that past day
L1  the Send button acted for a profile that was switched away meanwhile
L2  Settings' calendar count was labelled with the profile active at the end
Ported from the reviewer's probes; each failed before its fix.
"""
from __future__ import annotations

import ast
import asyncio
import json
import threading
import time
import types
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app as app_module
import clock
import icu_calendar_push as icp
import training_planner as tp
from test_plan_api import _mk_plan_dict
from test_icu_push import stub  # noqa: F401  (fixture)
from test_icu_hold_28 import env, _week, _seed, D  # noqa: F401  (env is a fixture)

APP = Path(__file__).resolve().parent.parent / "src" / "app.py"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    (tmp_path / "current_plan.json").write_text(json.dumps(_mk_plan_dict(monday)))
    return tmp_path, monday


# ── H1 ───────────────────────────────────────────────────────────────────────

def test_no_async_endpoint_takes_the_blocking_plan_lock():
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        stack = list(ast.iter_child_nodes(fn))
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(n, ast.With) and any(
                    isinstance(i.context_expr, ast.Call)
                    and getattr(i.context_expr.func, "attr", "") == "plan_write_lock"
                    for i in n.items):
                bad.append(f"{fn.name} (line {n.lineno})")
            stack.extend(ast.iter_child_nodes(n))
    # Ours: the plan store through run_in_threadpool (contract 4.2); upstream
    # names its async lock here.
    assert bad == [], "use `await run_in_threadpool(tp.update_plan, ...)` in: " + ", ".join(bad)


def test_no_async_endpoint_reaches_the_blocking_plan_lock_through_a_helper():
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    funcs = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(n.name, []).append(n)

    def calls(node):
        out = set()
        stack = list(ast.iter_child_nodes(node))
        while stack:
            x = stack.pop()
            if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue                     # nested defs run elsewhere (threadpool)
            if isinstance(x, ast.Call):
                f = x.func
                out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None))
            stack.extend(ast.iter_child_nodes(x))
        return out

    def takes_lock(fn):
        for x in ast.walk(fn):
            if isinstance(x, ast.With) and any(
                    getattr(getattr(i.context_expr, "func", None), "attr", "") == "plan_write_lock"
                    for i in x.items):
                return True
        return "atomic_write_plan" in calls(fn)

    # Ours: the plan store's blocking entry points (training_planner) seed the
    # set, so a sync helper reaching update_plan counts as blocking too.
    blocking = {"update_plan", "plan_write_lock", "atomic_write_plan"} | {
        name for name, fns in funcs.items()
        if any(isinstance(f, ast.FunctionDef) and takes_lock(f) for f in fns)}
    grew = True
    while grew:
        grew = False
        for name, fns in funcs.items():
            if name not in blocking and any(isinstance(f, ast.FunctionDef) and calls(f) & blocking for f in fns):
                blocking.add(name); grew = True

    bad = []
    for name, fns in funcs.items():
        for f in fns:
            if not isinstance(f, ast.AsyncFunctionDef) or name == "lifespan":
                continue
            # Ours has no plan_write_lock_async: no call is exempt (upstream
            # exempted the body of `async with plan_write_lock_async()`).
            inside = set()
            stack = list(ast.iter_child_nodes(f))
            while stack:
                x = stack.pop()
                if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                if isinstance(x, ast.Call) and id(x) not in inside:
                    fn = x.func
                    nm = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                    if nm in blocking:
                        bad.append(f"{name} line {x.lineno} -> {nm}")
                stack.extend(ast.iter_child_nodes(x))
    assert bad == [], bad


def test_a_waiting_plan_endpoint_does_not_freeze_other_requests(plan_dir):
    tmp, monday = plan_dir
    tue = (monday + timedelta(days=1)).isoformat()
    held = threading.Event()

    def background_writer():
        with tp.plan_write_lock():
            held.set()
            time.sleep(1.5)

    async def main():
        transport = httpx.ASGITransport(app=app_module.app)
        # Our line answers only a loopback Host (http://t is refused, 400).
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            async def dismiss():
                return await c.post("/api/plan/dismiss-session", json={"date": tue})

            async def version():
                await asyncio.sleep(0.2)
                r = await c.get("/api/version")
                return r, time.monotonic() - (t_start + 0.2)
            t_start = time.monotonic()
            return await asyncio.gather(dismiss(), version())

    t = threading.Thread(target=background_writer, daemon=True)
    t.start()
    assert held.wait(5)
    (rd, (rv, lat)) = asyncio.run(main())
    t.join(5)
    assert rv.status_code == 200 and lat < 0.5, f"/api/version waited {lat:.2f}s"
    assert rd.status_code == 200, rd.text             # and the dismiss still went through
    plan = json.loads((tmp / "current_plan.json").read_text())
    assert any(s.get("status") == "dismissed" for w in plan["weeks"] for s in w["sessions"]
               if s["day"] == tue)


# ── M1 ───────────────────────────────────────────────────────────────────────

def test_auto_recalc_reads_intervals_icu_before_taking_the_plan_lock(plan_dir, monkeypatch):
    tmp, _monday = plan_dir
    p = json.loads((tmp / "current_plan.json").read_text())
    p["recalc_date"] = "2026-01-01T00:00:00"
    (tmp / "current_plan.json").write_text(json.dumps(p))
    app_module.clear_cache()
    seen = {}

    def fake_fetch_wellness(days=42):
        seen["locked"] = tp._plan_write_lock._is_owned()
        return []

    monkeypatch.setattr(app_module, "fetch_wellness", fake_fetch_wellness)
    TestClient(app_module.app).post("/api/plan/auto-recalc")
    assert seen == {"locked": False}


def test_auto_recalc_skips_when_the_plan_was_rebuilt_while_it_read(plan_dir, monkeypatch):
    tmp, _monday = plan_dir
    path = tmp / "current_plan.json"
    p = json.loads(path.read_text())
    p["recalc_date"] = "2026-01-01T00:00:00"
    path.write_text(json.dumps(p))
    app_module.clear_cache()

    landed = {}

    def a_regenerate_lands(days=42):
        q = json.loads(path.read_text())
        q["recalc_date"] = clock.now().isoformat()
        path.write_text(json.dumps(q))
        landed["text"] = path.read_text()
        return []

    monkeypatch.setattr(app_module, "fetch_wellness", a_regenerate_lands)
    r = TestClient(app_module.app).post("/api/plan/auto-recalc").json()
    assert r["action"] == "fresh"
    after, then = json.loads(path.read_text()), json.loads(landed["text"])
    # (the plan heal may fill blank workout files; no second recalc ran)
    assert after["recalc_date"] == then["recalc_date"] and "recalc_info" not in after


# ── M2 ───────────────────────────────────────────────────────────────────────

def _ours(fk):
    return [e for e in fk.events if str(e.get("external_id") or "").startswith("domestique:")]


def test_a_reconcile_queued_before_sync_off_does_not_push_back(env, monkeypatch):
    env.pm.save_prefs({"icu_calendar_sync": True})
    _seed(env, _week(mon="steady.zwo", wed="sweet.zwo"))
    assert len(_ours(env.fk)) == 2
    arrived, go = threading.Event(), threading.Event()
    real = icp._one_at_a_time

    def gated(timeout_s, fn, *args):
        if fn.__name__ == "_reconcile_unless_swept":
            arrived.set()
            go.wait(10)                    # the sweep wins the lock first
        return real(timeout_s, fn, *args)

    monkeypatch.setattr(icp, "_one_at_a_time", gated)
    monkeypatch.setattr(app_module, "_icu_push_last_result", {})   # ours: per profile
    t = threading.Thread(target=app_module._icu_push_debounced_run, args=(env.pid,))
    t.start()
    assert arrived.wait(5)
    r = env.client.post("/api/icu/push", json={"sync": False})
    assert r.json().get("sync_enabled") is False and _ours(env.fk) == []
    go.set()
    t.join(10)
    assert _ours(env.fk) == []
    assert env.pid not in app_module._icu_push_last_result   # not shown as a result
    monkeypatch.setattr(icp, "_one_at_a_time", real)
    assert icp.reconcile()["pushed"] == 2                  # a new run still works


# ── M3 ───────────────────────────────────────────────────────────────────────

def test_a_swap_from_a_missed_past_day_is_refused(tmp_path, monkeypatch, freeze_clock):
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    mon = date(2026, 10, 12)
    freeze_clock(mon + timedelta(days=2))                  # Wednesday
    days = [(mon + timedelta(days=i)).isoformat() for i in range(7)]

    def s(day, st, status="pending"):
        return {"day": day, "day_name": date.fromisoformat(day).strftime("%a"),
                "session_type": st, "duration_min": 0 if st == "rest" else 60,
                "tss_estimate": 0 if st == "rest" else 60, "description": st,
                "zwo_file": "" if st == "rest" else f"{st}.zwo", "status": status}

    types_ = ["z2", "rest", "rest", "vo2max", "rest", "rest", "rest"]
    ss = [s(d, t, "missed" if i == 0 else "pending") for i, (d, t) in enumerate(zip(days, types_))]
    plan = {"goal": {"rest_days": [6]}, "availability": {},
            "weeks": [{"week_num": 1, "start": days[0], "end": days[6], "phase": "base",
                       "tss_target": 0, "is_stepback": False, "sessions": ss}]}
    path = tmp_path / "current_plan.json"
    path.write_text(json.dumps(plan))
    before = path.read_text()
    client = TestClient(app_module.app)
    r = client.post("/api/plan/move-session", json={"date": days[0], "new_date": days[3]})
    assert r.status_code == 409 and "day that has passed" in r.json()["error"]
    assert path.read_text() == before
    # Moving the missed workout onto a rest day still works.
    r = client.post("/api/plan/move-session", json={"date": days[0], "new_date": days[4]})
    assert r.status_code == 200, r.text


# ── L1, L2 ───────────────────────────────────────────────────────────────────

def test_send_after_a_profile_switch_during_the_wait_pushes_nothing(env, monkeypatch):
    from test_icu_hold_28 import _write_plan
    _write_plan(_week(wed="steady.zwo"))
    real = icp._one_at_a_time
    switched = types.SimpleNamespace(active_id="someone-else")

    def switch_then_run(timeout_s, fn, *args):
        monkeypatch.setattr(type(env.pm), "get", classmethod(lambda cls: switched))
        return real(timeout_s, fn, *args)

    monkeypatch.setattr(icp, "_one_at_a_time", switch_then_run)
    r = env.client.post("/api/calendar/push-workout", json={
        "source": "planner", "zwo_file": "steady.zwo", "date": D["wed"]}).json()
    assert r == {"error": "profile_changed"} and env.fk.calls == []


def test_settings_count_is_labelled_with_the_profile_the_run_was_for(monkeypatch):
    import profile_manager
    monkeypatch.setattr(profile_manager.ProfileManager, "get",
                        classmethod(lambda cls: types.SimpleNamespace(active_id="ben")))
    # Ours: results are kept per profile (contract 5.7); the run's own
    # profile_id names the slot, not the profile active when it lands.
    monkeypatch.setattr(app_module, "_icu_push_last_result", {})
    app_module._icu_push_note_result({"pushed": 0, "held": 2, "profile_id": "anna"})
    assert app_module._icu_push_last_result["anna"]["held"] == 2
    assert app_module._icu_push_last_result.get("ben") is None
    # A caller's pid captured before a switch never relabels the run either.
    app_module._icu_push_note_result({"pushed": 1, "profile_id": "anna"}, "ben")
    assert app_module._icu_push_last_result["anna"]["pushed"] == 1
    assert app_module._icu_push_last_result.get("ben") is None



def test_a_manual_push_behind_a_sweep_still_runs(env):
    """Grill L1: the rider's own Push after a sweep must not come back as
    "swept" ("couldn't reach intervals.icu")."""
    from test_icu_hold_28 import _write_plan
    _write_plan(_week(wed="steady.zwo"))
    stale = icp._SWEEP_GENERATION
    icp.sweep_all()                                       # bumps the generation
    assert icp._reconcile_unless_swept(14, stale)["error"] == "swept"   # background
    assert icp.reconcile(abort_if_swept=False)["pushed"] == 1           # rider's Push
