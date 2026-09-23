"""Contracts 4.1 / 4.2: one plan store.

tp.update_plan(json_path, mutate) holds the plan-write lock across read ->
mutate -> write. Before it existed, every writer read the plan with no lock
(or with the lock but racing an unlocked writer) and wrote its copy back: a
write that landed in between was overwritten with the stale copy (audit
2026-09-23, rv-data #3: a move-session lost to auto-recalc; the locked
auto-adapt's write lost to an unlocked move).

The first half pins the store itself and the planner's own read-modify-write
(rewrite_stale_plan_classifications). The second half is the app-level
contract; it needs app.py's writers moved onto the store (Wave 2, see
notes/hardening-interfaces-planio.md) and is marked strict-xfail until then,
so the change that makes it pass must also remove the marker.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

import clock
import training_planner as tp


def _read(p: Path) -> dict:
    return json.loads(p.read_text())


# ── the store ────────────────────────────────────────────────────────────────

def test_two_racing_read_modify_writes_both_persist(tmp_path: Path) -> None:
    """A holds the store mid-mutation; B's update waits and applies on top."""
    p = tmp_path / "current_plan.json"
    tp.atomic_write_plan(p, {"weeks": [], "n": 0})
    a_inside = threading.Event()
    b_done = threading.Event()

    def slow_a(plan):
        a_inside.set()
        b_done.wait(0.5)            # B gets every chance to write in between
        plan["a"] = True
        return plan

    def b(plan):
        plan["b"] = True
        return plan

    ta = threading.Thread(target=tp.update_plan, args=(p, slow_a))
    ta.start()
    assert a_inside.wait(5)
    tb = threading.Thread(target=lambda: (tp.update_plan(p, b), b_done.set()))
    tb.start()
    ta.join(10)
    tb.join(10)
    got = _read(p)
    assert got.get("a") is True and got.get("b") is True, got


def test_mutate_sees_the_file_and_none_writes_nothing(tmp_path: Path) -> None:
    p = tmp_path / "current_plan.json"
    assert tp.update_plan(p, lambda plan: None) is None      # missing file
    seen = []
    assert tp.update_plan(p, lambda plan: seen.append(plan)) is None
    assert seen == [None] and not p.exists()

    tp.atomic_write_plan(p, {"v": 1})
    before = p.stat().st_mtime_ns
    assert tp.update_plan(p, lambda plan: None) is None
    assert p.stat().st_mtime_ns == before
    assert not p.with_suffix(".json.bak").exists()           # nothing rotated

    out = tp.update_plan(p, lambda plan: {**plan, "v": plan["v"] + 1})
    assert out == {"v": 2} and _read(p) == {"v": 2}


def test_mutate_raising_writes_nothing_and_releases_the_lock(tmp_path: Path) -> None:
    p = tmp_path / "current_plan.json"
    tp.atomic_write_plan(p, {"v": 1})

    def boom(plan):
        plan["v"] = 99
        raise LookupError("no session at that date")

    with pytest.raises(LookupError):
        tp.update_plan(p, boom)
    assert _read(p) == {"v": 1}
    # the lock is free again: another thread can take it at once
    got = []

    def take():
        ok = tp._plan_write_lock.acquire(timeout=1)
        got.append(ok)
        if ok:
            tp._plan_write_lock.release()
    t = threading.Thread(target=take)
    t.start()
    t.join(5)
    assert got == [True]


# ── the planner's own read-modify-write ──────────────────────────────────────

def test_rewrite_stale_does_not_overwrite_a_concurrent_write(tmp_path, monkeypatch) -> None:
    """rewrite_stale_plan_classifications read the plan with no lock, spent
    the library match, then wrote its copy: a move landing in between was
    lost. Now its read, heal and write are one store call."""
    p = tmp_path / "current_plan.json"
    future = (clock.today() + timedelta(days=3)).isoformat()
    tp.atomic_write_plan(p, {
        "generated": clock.today().isoformat(),
        "weeks": [{"week_num": 1, "sessions": [
            {"day": future, "session_type": "z2", "zwo_file": "gone_z2.zwo",
             "zwo_name": "", "duration_min": 60, "tss_estimate": 40}]}]})
    monkeypatch.setattr(tp, "load_workout_library", lambda: [{"File": "z2_new.zwo"}])
    in_match = threading.Event()
    other_done = threading.Event()

    def slow_match(ps, library, **kw):
        in_match.set()
        other_done.wait(0.5)
        ps.zwo_file, ps.zwo_name = "z2_new.zwo", "Z2 new"
    monkeypatch.setattr(tp, "match_zwo", slow_match)

    counts = []
    t1 = threading.Thread(target=lambda: counts.append(tp.rewrite_stale_plan_classifications(p)))
    t1.start()
    assert in_match.wait(5)

    def other(plan):
        plan["moved_by_rider"] = True
        return plan
    t2 = threading.Thread(target=lambda: (tp.update_plan(p, other), other_done.set()))
    t2.start()
    t1.join(10)
    t2.join(10)
    got = _read(p)
    assert counts == [1]
    assert got["weeks"][0]["sessions"][0]["zwo_file"] == "z2_new.zwo"
    assert got.get("moved_by_rider") is True, "the concurrent write was overwritten"


# ── the app-level contract (Wave 2) ──────────────────────────────────────────

def _week(monday, n):
    types = ["rest", "z2", "tempo", "z2", "rest", "long_z2", "rest"]
    dur = [0, 60, 75, 60, 0, 120, 0]
    return {"week_num": n, "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(), "phase": "base",
            "tss_target": 300, "is_stepback": False,
            "sessions": [{"day": (monday + timedelta(days=i)).isoformat(),
                          "day_name": (monday + timedelta(days=i)).strftime("%a"),
                          "session_type": types[i], "duration_min": dur[i],
                          "tss_estimate": dur[i] * 0.8, "description": types[i],
                          "zwo_file": "", "zwo_name": "", "status": "pending"}
                         for i in range(7)]}


@pytest.fixture
def app_plan(tmp_path, monkeypatch):
    import app as A
    today = clock.today()
    mon = today - timedelta(days=today.weekday())
    stale = (clock.now() - timedelta(days=10)).isoformat()
    plan = {"goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0, 4, 6],
                     "target_date": (mon + timedelta(weeks=8)).isoformat()},
            "phases": [], "weeks": [_week(mon + timedelta(weeks=k), k + 1) for k in range(8)],
            "generated": stale, "recalc_date": stale}
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    jp = tmp_path / "current_plan.json"
    jp.write_text(json.dumps(plan))
    monkeypatch.setattr(A, "get_today_metrics", lambda *a, **k: {})
    monkeypatch.setattr(A, "fetch_wellness", lambda *a, **k: [])
    src = (mon + timedelta(weeks=1, days=1)).isoformat()
    dst = (mon + timedelta(weeks=1, days=3)).isoformat()

    def moved_on_disk():
        return any(s["day"] == dst and s.get("user_moved")
                   for w in _read(jp)["weeks"] for s in w["sessions"])
    return A, jp, src, dst, moved_on_disk


def test_move_racing_auto_recalc_both_persist(app_plan, monkeypatch) -> None:
    """4.1: auto-recalc (threadpool) is inside its rebuild when a
    move-session arrives; after both return, the move is on disk."""
    from fastapi.testclient import TestClient
    A, jp, src, dst, moved_on_disk = app_plan
    inside, release = threading.Event(), threading.Event()

    def slow_recalc(*a, current_plan_weeks, **k):
        inside.set()
        release.wait(10)
        return [], current_plan_weeks, {"action": "recalculated"}
    monkeypatch.setattr(tp, "recalculate_plan", slow_recalc)

    c = TestClient(A.app, base_url="http://127.0.0.1")
    res = {}
    t1 = threading.Thread(target=lambda: res.setdefault("recalc", c.post("/api/plan/auto-recalc")))
    t1.start()
    assert inside.wait(20), "auto-recalc never reached recalculate_plan"
    t2 = threading.Thread(target=lambda: res.setdefault(
        "move", c.post("/api/plan/move-session", json={"date": src, "new_date": dst})))
    t2.start()
    t2.join(1.0)            # unfixed: the move finishes here; fixed: it waits on the store
    release.set()
    t1.join(30)
    t2.join(30)
    assert res["recalc"].status_code == 200, res["recalc"].text
    assert res["move"].status_code == 200, res["move"].text
    assert res["recalc"].json().get("action") == "recalculated"
    assert moved_on_disk(), "the move was overwritten by the recalc's stale copy"
    # ...and the recalc was not overwritten by the move's stale copy either
    assert _read(jp)["recalc_date"][:10] == clock.today().isoformat(), \
        "the recalc was overwritten by the move's stale copy"


def test_event_loop_answers_while_a_slow_writer_holds_the_plan(app_plan) -> None:
    """4.2: a move-session waiting on the plan lock must wait in the
    threadpool, not on the event loop: /api/version answers in < 0.5 s."""
    import anyio.from_thread
    from fastapi.testclient import TestClient
    A, jp, src, dst, moved_on_disk = app_plan
    c = TestClient(A.app, base_url="http://127.0.0.1")
    res = {}
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal                      # one event loop for every request
        held, done = threading.Event(), threading.Event()

        def slow_writer():
            with tp.plan_write_lock():
                held.set()
                done.wait(3)
        tw = threading.Thread(target=slow_writer)
        tw.start()
        assert held.wait(5)
        tm = threading.Thread(target=lambda: res.setdefault(
            "move", c.post("/api/plan/move-session", json={"date": src, "new_date": dst})))
        tm.start()
        time.sleep(0.3)                        # the move is now waiting on the lock
        t0 = time.monotonic()
        r = c.get("/api/version")
        elapsed = time.monotonic() - t0
        done.set()
        tw.join(10)
        tm.join(30)
        c.portal = None
    assert r.status_code == 200
    assert res["move"].status_code == 200
    assert elapsed < 0.5, f"/api/version took {elapsed:.2f}s behind the plan lock"


# ── fix-forward F1: the training fetch never runs on the event loop ─────────

def _training_readers():
    race = (clock.today() + timedelta(weeks=3)).isoformat()
    return [
        ("/api/readiness/apply-tier-down", {}),
        ("/api/plan/auto-adjust", {"scope": "today"}),
        ("/api/plan/generate", {"goal": "general", "hours_per_week": 7.0,
                                "plan_weeks": 4, "rest_days": [0]}),
        ("/api/plan/reforecast", None),
        ("/api/plan/save-availability", {"availability": {}}),
        ("/api/plan/regenerate", {}),
        ("/api/plan/add-race", {"date": race, "priority": "B"}),
        ("/api/plan/update", None),
    ]


def test_no_async_handler_reads_the_training_metrics_on_the_event_loop(app_plan, monkeypatch) -> None:
    """The eight async handlers that need CTL/TSB read
    `cached("training", get_today_metrics)` -- an intervals.icu fetch on a
    cold or expired cache -- on the event loop: during an outage every
    request on the server waited for it (fix-forward F1: /api/version 12 s).
    Each route must read it, and never from the loop's thread."""
    import asyncio
    import cache
    from fastapi.testclient import TestClient
    A, jp, *_ = app_plan
    original = jp.read_text()
    where = []

    def metrics(*a, **k):
        try:
            asyncio.get_running_loop()
            where.append("loop")
        except RuntimeError:
            where.append("pool")
        return {"ctl": 50.0, "atl": 45.0, "tsb": 5.0}
    monkeypatch.setattr(A, "get_today_metrics", metrics)
    c = TestClient(A.app, base_url="http://127.0.0.1")
    for route, body in _training_readers():
        jp.write_text(original)
        cache.clear_cache()
        where.clear()
        r = c.post(route) if body is None else c.post(route, json=body)
        assert r.status_code < 500, (route, r.status_code, r.text[:200])
        assert where, f"{route} never read the training metrics (test is vacuous)"
        assert "loop" not in where, f"{route} fetched the training metrics on the event loop"


def test_an_outage_neither_stalls_the_loop_nor_asks_per_request(app_plan, monkeypatch, request) -> None:
    """The evaluator's probe (p_loop_stall): intervals.icu takes 1 s to fail,
    the training cache holds an expired good value, three reforecasts arrive.
    slim/6: ICU asked once, /api/version 2.1 s behind it; unfixed branch:
    asked 6x, /api/version 12 s. Now: asked once, the loop never waits."""
    import anyio.from_thread
    import cache
    import config
    import training as tr
    from fastapi.testclient import TestClient
    A = app_plan[0]
    monkeypatch.setattr(A, "get_today_metrics", tr.get_today_metrics)
    # Not monkeypatch: config resolves ICU_* through a module __getattr__
    # (see test_hardening_metrics_cache's fixture).
    config.ICU_ATHLETE_ID, config.ICU_API_KEY = "i1", "k"
    request.addfinalizer(lambda: [config.__dict__.pop(k, None)
                                  for k in ("ICU_ATHLETE_ID", "ICU_API_KEY")])
    state = {"up": True, "asks": 0}

    def wellness(days=42):
        state["asks"] += 1
        if state["up"]:
            return [{"id": clock.today().isoformat(), "ctl": 60.0, "atl": 55.0, "sportInfo": []}]
        time.sleep(1.0)
        raise tr.ICUNetworkError("timed out")
    monkeypatch.setattr(tr, "fetch_wellness", wellness)
    monkeypatch.setattr(tr, "fetch_activities", lambda days=7: [])
    cache.clear_cache()
    assert cache.cached("training", tr.get_today_metrics)["ctl"] == 60.0
    cache._cache_ts["training"] -= 301
    state.update(up=False, asks=0)
    c = TestClient(A.app, base_url="http://127.0.0.1")
    lat, codes, stop = [], [], threading.Event()
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        def poll():
            while not stop.is_set():
                t0 = time.monotonic()
                c.get("/api/version")
                lat.append(time.monotonic() - t0)
                time.sleep(0.05)
        p = threading.Thread(target=poll)
        p.start()
        time.sleep(0.2)
        ws = [threading.Thread(target=lambda: codes.append(c.post("/api/plan/reforecast").status_code))
              for _ in range(3)]
        [w.start() for w in ws]
        [w.join(60) for w in ws]
        stop.set()
        p.join(10)
        c.portal = None
    cache.clear_cache()
    assert codes == [200] * 3, codes
    assert state["asks"] == 1, f"intervals.icu asked {state['asks']}x for one outage window"
    assert max(lat) < 0.5, f"/api/version waited {max(lat):.2f}s behind the outage"


# ── fix-forward F6: the rule is enforced at runtime, not by pattern ─────────

@pytest.mark.parametrize("how", ["update_plan", "atomic_write_plan",
                                 "plan_write_lock", "raw_lock"])
def test_the_plan_store_refuses_the_event_loop_and_names_the_caller(tmp_path, how) -> None:
    """The AST guard missed 7 of 12 planted violations (an alias, a nested
    def, a helper in another module, a from-import, a method, the raw lock
    attribute...; two of them stalled the loop 2.6 s). The store itself now
    refuses to be entered from a thread running an event loop, whoever the
    caller is, and says who called it."""
    import asyncio
    p = tmp_path / "current_plan.json"
    tp.atomic_write_plan(p, {"weeks": [], "v": 1})
    def with_lock(lock):
        with lock:
            pass
    calls = {
        "update_plan": lambda: tp.update_plan(p, lambda plan: {**plan, "v": 2}),
        "atomic_write_plan": lambda: tp.atomic_write_plan(p, {"weeks": [], "v": 2}),
        "plan_write_lock": lambda: with_lock(tp.plan_write_lock()),
        "raw_lock": lambda: with_lock(tp._plan_write_lock),
    }

    async def planted_handler():
        calls[how]()
    with pytest.raises(RuntimeError, match="planted_handler"):
        asyncio.run(planted_handler())
    assert _read(p) == {"weeks": [], "v": 1}

    async def control():                   # the sanctioned way still works
        return await asyncio.to_thread(tp.update_plan, p, lambda plan: {**plan, "v": 3})
    assert asyncio.run(control())["v"] == 3


def _plan_writer_routes(src, dst):
    today = clock.today()
    mon = today - timedelta(days=today.weekday())
    nxt = (mon + timedelta(weeks=1, days=2)).isoformat()
    race = (today + timedelta(weeks=3)).isoformat()
    return {
        "api_readiness_revert_cap": ("/api/readiness/revert-cap", {}),
        "api_readiness_apply_tier_down": ("/api/readiness/apply-tier-down",
                                          {"date": (mon + timedelta(days=2)).isoformat()}),
        "api_plan_auto_adjust": ("/api/plan/auto-adjust", {"scope": "today", "severity": "rest"}),
        "api_today_session_persist": ("/api/today-session/persist",
                                      {"session_type": "z2", "reason": "t"}),
        "api_continuous_deload_revert": ("/api/plan/continuous/deload-revert", {}),
        "api_plan_generate": ("/api/plan/generate", {"goal": "general", "hours_per_week": 7.0,
                                                     "plan_weeks": 4, "rest_days": [0]}),
        "api_plan_reforecast": ("/api/plan/reforecast", None),
        "api_mark_unavailable": ("/api/plan/mark-unavailable",
                                 {"start": nxt, "end": nxt, "type": "holiday"}),
        "api_save_availability": ("/api/plan/save-availability", {"availability": {}}),
        "api_plan_regenerate_dynamic": ("/api/plan/regenerate", {}),
        "api_plan_add_race": ("/api/plan/add-race", {"date": race}),
        "api_plan_update": ("/api/plan/update", None),
        "api_plan_move_session": ("/api/plan/move-session", {"date": src, "new_date": dst}),
        "api_plan_rematch": ("/api/plan/rematch?apply=1", None),
        "api_plan_re_draw": ("/api/plan/re-draw", {"date": nxt}),
        "api_plan_accept_redraw": ("/api/plan/accept-redraw", {"date": nxt, "zwo_file": "x.zwo"}),
        "api_plan_swap_type": ("/api/plan/swap-type",
                               {"date": nxt, "session_type": "z2", "duration_min": 60}),
        "api_plan_ftp_test_type": ("/api/plan/ftp-test-type", {"date": nxt, "test_type": "ramp"}),
        "api_plan_dismiss_session": ("/api/plan/dismiss-session", {"date": nxt}),
        "api_plan_auto_recalc": ("/api/plan/auto-recalc", None),
    }


def test_every_plan_writer_route_waits_off_the_event_loop(app_plan, monkeypatch) -> None:
    """Hold the plan lock, send every plan-writing route at once: each one
    reaches the store from a worker thread, none answers 500 (the runtime
    guard's RuntimeError), and /api/version answers in < 0.5 s meanwhile."""
    import asyncio
    import anyio.from_thread
    from fastapi.testclient import TestClient
    A, jp, src, dst, _ = app_plan
    routes = _plan_writer_routes(src, dst)
    entered, real = [], tp.update_plan

    def spy(p, mutate):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        entered.append((mutate.__qualname__.split(".")[0], on_loop))
        return real(p, mutate)
    monkeypatch.setattr(tp, "update_plan", spy)
    c = TestClient(A.app, base_url="http://127.0.0.1")
    res, lat = {}, []
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        held, done = threading.Event(), threading.Event()

        def slow_writer():
            with tp.plan_write_lock():
                held.set()
                done.wait(30)
        tw = threading.Thread(target=slow_writer)
        tw.start()
        assert held.wait(5)
        ts = []
        for name, (url, body) in routes.items():
            send = (lambda u=url, b=body, n=name: res.setdefault(
                n, c.post(u) if b is None else c.post(u, json=b)))
            ts.append(threading.Thread(target=send))
            ts[-1].start()
        deadline = time.monotonic() + 20
        while len(entered) < len(routes) and time.monotonic() < deadline:
            time.sleep(0.05)
        for _ in range(3):
            t0 = time.monotonic()
            c.get("/api/version")
            lat.append(time.monotonic() - t0)
        done.set()
        tw.join(10)
        for t in ts:
            t.join(60)
        c.portal = None
    missing = set(routes) - {n for n, _ in entered}
    assert not missing, f"routes that never reached the store (test is vacuous): {sorted(missing)}"
    assert [n for n, on_loop in entered if on_loop] == []
    bad = {n: (r.status_code, r.text[:120]) for n, r in res.items() if r.status_code >= 500}
    assert bad == {}
    assert max(lat) < 0.5, f"/api/version took {max(lat):.2f}s behind the plan lock"


def test_the_boot_passes_still_heal_the_plan_under_the_guard() -> None:
    """lifespan runs on the event loop: its plan writers (the legacy-plan
    adoption, the stale-classification rewrite, the self-heal) go through
    the threadpool, or the guard refuses them and -- best-effort, the
    errors are swallowed -- the plan is silently never healed."""
    import app as A
    from fastapi.testclient import TestClient
    from profile_manager import ProfileManager
    pm = ProfileManager.get()
    if pm.active_id is None:
        pm.switch(pm.create_profile("Boot"))
    jp = Path(pm.plan_dir) / "current_plan.json"      # what lifespan points the app at
    jp.parent.mkdir(parents=True, exist_ok=True)
    today = clock.today()
    mon = today - timedelta(days=today.weekday())
    tp.atomic_write_plan(jp, {"goal": {"type": "general", "hours_per_week": 8.0},
                              "phases": [], "generated": clock.now().isoformat(),
                              "weeks": [_week(mon + timedelta(weeks=k), k + 1) for k in range(3)]})
    with TestClient(A.app, base_url="http://127.0.0.1"):
        pass
    future = [s for w in _read(jp)["weeks"] for s in w["sessions"]
              if s["day"] > today.isoformat() and s["session_type"] != "rest"]
    assert future and all(s["zwo_file"] for s in future), \
        [s["day"] for s in future if not s["zwo_file"]]
