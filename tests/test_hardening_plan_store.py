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

WAVE2 = pytest.mark.xfail(
    strict=True,
    reason="Wave 2: app.py's plan writers move onto tp.update_plan "
           "(notes/hardening-interfaces-planio.md); remove this marker then")


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


@WAVE2
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


@WAVE2
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
