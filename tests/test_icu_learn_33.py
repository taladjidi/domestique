"""#33 (#28 step 2) — a plain move the rider makes in the intervals.icu
calendar is learned into the plan, as if dragged in Domestique.

Learned: one event under its id, both days today or later in one week, from
a day holding that one pending workout onto a rest/empty/vacated day that is
not unavailable and not a race. The event is then re-keyed by the agreed-move
rule in the same run. Everything else stays held (#28 step 1). Never learned
when most of our events look shifted (a date-reading problem, not moves).
"""
from __future__ import annotations

import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

import clock
import icu_calendar_push as icp
import training_planner as tp
from test_icu_push import stub, _write_plan, _apikey, _eid, TEMPO_ZWO, STEADY_ZWO  # noqa: F401
from test_icu_hold_28 import (  # noqa: E402
    WindowedFakeICU, MON, D, _week, _move, _one_event_per_session, _counts, _at,
)
from test_331_surfaces import _extract_js_function, _run_node

DASHBOARD = Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"


@pytest.fixture()
def env(stub, freeze_clock, monkeypatch):
    freeze_clock(MON)
    _apikey(stub)
    (stub.workouts / "sweet.zwo").write_text(TEMPO_ZWO, encoding="utf-8")
    (stub.workouts / "vo2.zwo").write_text(STEADY_ZWO, encoding="utf-8")
    stub.fk = WindowedFakeICU()
    monkeypatch.setattr(icp, "_http", stub.fk)
    return stub


def _plan():
    return json.loads((Path(tp.PLAN_DIR) / "current_plan.json").read_text())


def _on(day):
    return [s for w in _plan()["weeks"] for s in w["sessions"] if s["day"] == day]


def _seed(env, ss):
    _write_plan(ss)
    r = icp.reconcile()
    assert not r.get("error") and r["learned"] == [] and r["held"] == 0, r
    _later()


def _later():
    """The rider drags minutes after our push, not in the same instant: the
    recent-push guard (an event we just wrote read back elsewhere is a
    reading problem) must not see the drag as one."""
    icp._RECENT_PUSH.clear()


@pytest.fixture(autouse=True)
def _fresh_push_memory():
    icp._RECENT_PUSH.clear()
    yield
    icp._RECENT_PUSH.clear()


@pytest.mark.parametrize("echo", [True, False])
def test_a_drag_onto_a_rest_day_is_learned_and_the_event_rekeyed(env, echo):
    env.fk.echo_filename = echo
    _seed(env, _week(mon="sweet.zwo", wed="steady.zwo"))
    wed0, fri0 = _eid(env, D["wed"]), _eid(env, D["fri"])
    env.fk.drag(wed0, D["fri"])
    r = icp.reconcile()
    assert [(m["from"], m["to"]) for m in r["learned"]] == [(D["wed"], D["fri"])]
    assert r["held"] == 0 and not r.get("error")
    fri, = _on(D["fri"])
    assert (fri["zwo_file"], fri["user_moved"], fri["moved_from"]) == ("steady.zwo", True, D["wed"])
    wed, = _on(D["wed"])
    assert wed["status"] == f"moved_from:{D['fri']}"
    assert env.fk.where(fri0) == [D["fri"]] and env.fk.where(wed0) == []
    assert env.fk.files_on() == {D["mon"]: ["sweet"], D["fri"]: ["steady"]}
    before = _plan()
    r2 = icp.reconcile()                                  # settled: nothing moves
    assert r2["learned"] == [] and _counts(r2) == (0, 0, 0) and _plan() == before
    _one_event_per_session(env)


def test_a_drag_to_today_is_learned_like_any_move(env):
    """Owner (#33): the plan follows a move to today. The event is re-keyed
    like any learned move (keeping the old id blocked the vacated day for
    every later workout, grill r4 M3)."""
    _seed(env, _week(mon="sweet.zwo", thu="steady.zwo", sat="vo2.zwo"))
    env.fk.drag(_eid(env, D["mon"]), D["tue"])            # Monday's goes to Tue
    icp.reconcile()
    _later()
    env.fk.drag(_eid(env, D["thu"]), D["mon"])            # "ride Thursday's today"
    r = icp.reconcile()
    assert [(m["from"], m["to"]) for m in r["learned"]] == [(D["thu"], D["mon"])]
    mon, = _on(D["mon"])
    assert (mon["zwo_file"], mon["user_moved"], mon["moved_from"]) == ("steady.zwo", True, D["thu"])
    assert env.fk.where(_eid(env, D["mon"])) == [D["mon"]] and env.fk.where(_eid(env, D["thu"])) == []
    assert r["held"] == 0
    _one_event_per_session(env)
    assert icp.reconcile()["learned"] == []


def test_a_workout_put_on_the_day_a_move_left_reaches_the_watch(env):
    """Grill r4 M3: after a learned move, the day it left takes a new
    workout like any other day."""
    ss = _week(mon="sweet.zwo", thu="steady.zwo", sat="vo2.zwo")
    _seed(env, ss)
    env.fk.drag(_eid(env, D["thu"]), D["mon"])
    icp.reconcile()
    _later()
    plan = _plan()
    thu = next(x for w in plan["weeks"] for x in w["sessions"] if x["day"] == D["thu"])
    thu.update(session_type="z2", zwo_file="tempo_alt.zwo", status="pending",
               duration_min=60, tss_estimate=45)
    tp.atomic_write_plan(Path(tp.PLAN_DIR) / "current_plan.json", plan)
    icp.reconcile()
    assert env.fk.files_on().get(D["thu"]) == ["tempo_alt"]
    sent = env.client.post("/api/calendar/push-workout", json={
        "source": "planner", "zwo_file": "tempo_alt.zwo", "date": D["thu"]}).json()
    assert sent.get("pushed") == 1, sent


def test_a_move_right_after_an_unchanged_push_is_learned(env):
    """Grill r4 M1: every reconcile re-pushes; only events we put on a new
    day count as "just written"."""
    _seed(env, _week(mon="sweet.zwo", tue="vo2.zwo", wed="steady.zwo"))
    icp.reconcile()                                       # an unchanged re-push
    env.fk.drag(_eid(env, D["wed"]), D["fri"])            # the rider, right after
    r = icp.reconcile()
    assert [(m["from"], m["to"]) for m in r["learned"]] == [(D["wed"], D["fri"])]


@pytest.mark.parametrize("why,setup", [
    ("onto a training day", lambda env: env.fk.drag(_eid(env, D["wed"]), D["thu"])),
    ("into next week", lambda env: env.fk.drag(_eid(env, D["wed"]), (MON + timedelta(days=9)).isoformat())),
    ("a copy, two events one id", lambda env: env.fk.copy(_eid(env, D["wed"]), D["fri"])),
])
def test_moves_that_are_not_plain_stay_held_and_the_plan_untouched(env, why, setup):
    _seed(env, _week(wed="steady.zwo", thu="sweet.zwo"))
    before = _plan()
    setup(env)
    r = icp.reconcile()
    assert r["learned"] == [] and _plan() == before, why
    assert r["held"] == 1, why
    if "copy" not in why:
        _one_event_per_session(env)


def test_a_move_into_the_past_is_not_learned(env, freeze_clock):
    _seed(env, _week(wed="steady.zwo"))
    env.fk.drag(_eid(env, D["wed"]), (MON - timedelta(days=1)).isoformat())
    before = _plan()
    r = icp.reconcile()
    assert r["learned"] == [] and _plan() == before


@pytest.mark.parametrize("mark", ["zero_hours", "period", "race"])
def test_an_unavailable_or_race_day_is_not_learned(env, mark):
    ss = _week(wed="steady.zwo")
    _seed(env, ss)
    plan = _plan()
    if mark == "zero_hours":
        plan["availability"] = {D["fri"]: {"hours": 0, "type": "holiday"}}   # the stored shape
    elif mark == "period":
        plan["unavailable_periods"] = [{"start": D["thu"], "end": D["sat"], "type": "holiday"}]
    else:
        fri = next(s for w in plan["weeks"] for s in w["sessions"] if s["day"] == D["fri"])
        fri["is_race"] = True
    tp.atomic_write_plan(Path(tp.PLAN_DIR) / "current_plan.json", plan)
    env.fk.drag(_eid(env, D["wed"]), D["fri"])
    before = _plan()
    r = icp.reconcile()
    assert r["learned"] == [] and _plan() == before and r["held"] == 1


def test_a_day_domestique_changed_after_the_move_is_not_learned(env):
    """Owner rule: Domestique's own later change of that day wins."""
    ss = _week(wed="steady.zwo")
    _seed(env, ss)
    env.fk.drag(_eid(env, D["wed"]), D["fri"])
    ss[_at(ss, D["wed"])]["zwo_file"] = "sweet.zwo"       # a different workout now
    _write_plan(ss)
    r = icp.reconcile()
    assert r["learned"] == []
    assert _on(D["wed"])[0]["zwo_file"] == "sweet.zwo"


def test_most_events_shifted_means_a_date_problem_and_nothing_is_learned(env):
    _seed(env, _week(mon="sweet.zwo", wed="steady.zwo", fri="vo2.zwo"))
    for day, nxt in (("mon", "tue"), ("wed", "thu"), ("fri", "sat")):   # each onto a rest day
        env.fk.drag(_eid(env, D[day]), D[nxt])
    before = _plan()
    r = icp.reconcile()
    assert r["learned"] == [] and _plan() == before


def test_a_skipped_workout_moved_in_icu_is_not_learned(env):
    ss = _week(wed="steady.zwo")
    _seed(env, ss)
    env.fk.drag(_eid(env, D["wed"]), D["fri"])
    ss[_at(ss, D["wed"])]["status"] = "dismissed"
    _write_plan(ss)
    assert icp.reconcile()["learned"] == []


def test_the_move_learned_is_pinned_like_a_drag(env):
    """It keeps its length when the day's hours change (#27 O4) because it
    is the rider's move; the auto-reschedule never moves it back."""
    _seed(env, _week(mon="sweet.zwo", wed="steady.zwo"))
    env.fk.drag(_eid(env, D["wed"]), D["fri"])
    icp.reconcile()
    fri, = _on(D["fri"])
    assert fri["user_moved"] is True and not fri.get("auto_moved")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_push_toast_says_what_was_learned():
    src = DASHBOARD.read_text(encoding="utf-8")
    harness = _extract_js_function(src, "icuPushToastFromResult") + """
const seen = [];
global.showToast = (m) => seen.push(m);
icuPushToastFromResult({pushed: 1, updated: 0, deleted: 1, skipped: [], held: 0,
                        learned: [{from: '2026-10-21', to: '2026-10-23', name: 'Z2'}]}, 'Pushed to intervals.icu');
const want = ['Pushed to intervals.icu: 1 new · 1 removed · 1 workout moved in your plan to match intervals.icu'];
if (JSON.stringify(seen) !== JSON.stringify(want)) throw new Error(JSON.stringify(seen));
console.log('OK');
"""
    _run_node(harness)



def _files_by_day():
    return {x["day"]: x.get("zwo_file") for w in _plan()["weeks"] for x in w["sessions"]
            if x.get("zwo_file")}


def test_two_moves_onto_the_same_rest_day_learn_neither(env):
    """Grill r2 M-A: the second move hit a training day and swapped."""
    _seed(env, _week(mon="vo2.zwo", wed="steady.zwo", thu="sweet.zwo", sat="vo2.zwo"))
    env.fk.drag(_eid(env, D["wed"]), D["fri"])
    env.fk.drag(_eid(env, D["thu"]), D["fri"])
    before = _plan()
    r = icp.reconcile()
    assert r["learned"] == [] and _plan() == before and r["held"] == 2


def test_a_profile_switch_during_the_read_never_writes_the_other_plan(env, monkeypatch):
    """Grill r2 H-B: rider A's move was written into rider B's plan."""
    _seed(env, _week(wed="steady.zwo"))
    pid_a = env.pid
    pid_b = env.pm.create_profile("Other rider")
    env.pm.switch(pid_b)
    _write_plan(_week(wed="steady.zwo"))
    b_path = Path(tp.PLAN_DIR) / "current_plan.json"
    b_before = b_path.read_text()
    env.pm.switch(pid_a)
    a_path = Path(tp.PLAN_DIR) / "current_plan.json"
    a_before = a_path.read_text()
    env.fk.drag(_eid(env, D["wed"]), D["fri"])
    fk, state = env.fk, {"switched": False}

    def http(method, path, payload=None, timeout=30.0):
        if method == "GET" and not state["switched"]:
            state["switched"] = True
            env.pm.switch(pid_b)
        return fk(method, path, payload, timeout)

    monkeypatch.setattr(icp, "_http", http)
    r = icp.reconcile()
    assert r["learned"] == []
    assert b_path.read_text() == b_before and a_path.read_text() == a_before


class _ShiftedFakeICU(WindowedFakeICU):
    """Fact F2 false: every date read back one day late."""
    def __call__(self, method, path, payload=None, timeout=30.0):
        status, body = super().__call__(method, path, payload, timeout)
        if method == "GET" and status == 200:
            rows = json.loads(body)
            for r in rows:
                d = str(r.get("start_date_local") or "")
                if len(d) >= 10:
                    r["start_date_local"] = (date.fromisoformat(d[:10]) + timedelta(days=1)).isoformat() + d[10:]
            body = json.dumps(rows).encode()
        return status, body


def test_the_only_event_of_ours_is_learned_too(env):
    """The date-shift guard needs two events to judge; a single event is
    protected by the midnight and recent-push checks instead."""
    _seed(env, _week(wed="steady.zwo"))
    env.fk.drag(_eid(env, D["wed"]), D["fri"])
    r = icp.reconcile()
    assert [(m["from"], m["to"]) for m in r["learned"]] == [(D["wed"], D["fri"])]


def test_a_date_read_back_shifted_never_walks_a_workout(env, monkeypatch):
    """Grill r2 M-C: with fewer than 3 events the guard never fired and the
    workout walked one day per run."""
    env.fk = _ShiftedFakeICU()
    monkeypatch.setattr(icp, "_http", env.fk)
    _write_plan(_week(tue="steady.zwo", sat="sweet.zwo"))
    days = []
    for _ in range(4):
        icp.reconcile()
        days.append(next(d for d, f in _files_by_day().items() if f == "steady.zwo"))
    assert days == [D["tue"]] * 4


def test_a_second_move_is_learned_too(env):
    """Owner (#33): a workout already moved once follows a second move."""
    _seed(env, _week(mon="sweet.zwo", tue="vo2.zwo", wed="steady.zwo"))
    env.fk.drag(_eid(env, D["wed"]), D["fri"])
    assert len(icp.reconcile()["learned"]) == 1
    _later()
    env.fk.drag(_eid(env, D["fri"]), D["sat"])
    r = icp.reconcile()
    assert [(m["from"], m["to"]) for m in r["learned"]] == [(D["fri"], D["sat"])]
    sat, = _on(D["sat"])
    assert sat["zwo_file"] == "steady.zwo" and sat["user_moved"] is True
    assert env.fk.files_on()[D["sat"]] == ["steady"]
    _one_event_per_session(env)


def test_our_own_push_read_back_elsewhere_is_not_a_move(env):
    """An event we wrote minutes ago that reads back on another day is a
    date-reading problem: learning it would walk the workout on."""
    _seed(env, _week(mon="sweet.zwo", tue="vo2.zwo", wed="steady.zwo"))
    icp.note_pushed([{"external_id": _eid(env, D["wed"]), "start_date_local": f"{D['wed']}T00:00:00"}])
    env.fk.drag(_eid(env, D["wed"]), D["fri"])
    before = _plan()
    r = icp.reconcile()
    assert r["learned"] == [] and _plan() == before and r["held"] == 1


def test_a_changed_time_of_day_is_not_a_move(env):
    """A timezone shift moves the hour; a drag keeps our midnight."""
    _seed(env, _week(mon="sweet.zwo", tue="vo2.zwo", wed="steady.zwo"))
    env.fk._one(_eid(env, D["wed"]))["start_date_local"] = f"{D['thu']}T01:00:00"
    before = _plan()
    r = icp.reconcile()
    assert r["learned"] == [] and _plan() == before


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_failed_push_after_learning_says_the_plan_changed():
    """Grill r2 L1: "nothing was changed" was wrong; the plan had moved."""
    src = DASHBOARD.read_text(encoding="utf-8")
    harness = _extract_js_function(src, "icuPushToastFromResult") + """
const seen = [];
global.showToast = (m) => seen.push(m);
icuPushToastFromResult({error: 'http_500', learned: [{from: 'a', to: 'b'}]}, 'Pushed');
icuPushToastFromResult({needs_reconnect: true, learned: [{from: 'a', to: 'b'}, {from: 'c', to: 'd'}]}, 'Pushed');
if (!seen[0].startsWith('Your plan now follows 1 move you made in intervals.icu')) throw new Error(seen[0]);
if (!seen[1].startsWith('Your plan now follows 2 moves') || !seen[1].includes('reconnect')) throw new Error(seen[1]);
console.log('OK');
"""
    _run_node(harness)
