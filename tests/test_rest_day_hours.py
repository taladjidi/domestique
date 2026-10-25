"""3.13.2 — hours set on a rest day make it a ride; the week makes room.

Owner report: today was a rest day, the rider gave it 1 h, it stayed rest.
Two causes: the week's load was already planned on other days (hours are a
ceiling under the week's budget, D5), and a day a move had vacated ignored
the hours altogether. Owner decision: ride today, and the rest of the week
is recalculated so the week keeps its load.

Only for days the rider changed in that request (save-availability): every
other pass (each sync hands over the whole availability calendar) keeps D5,
or every rest day with free hours would turn into a ride.

Every case runs through reforecast_dict, the path that writes the plan: the
prototype's tests called reforecast directly and missed that the write-back
dropped the restored day's status (IP_3.13.2 W1).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import clock
import icu_calendar_push as icp
import training_planner as tp
from test_icu_push import stub, _write_plan, _eid  # noqa: F401
from test_icu_hold_28 import D as HD, _week as _hold_week, _one_event_per_session  # noqa: F401
from test_icu_learn_33 import env, _seed, _on, _plan  # noqa: F401

TODAY = date(2026, 10, 19)           # a Monday
D = [(TODAY + timedelta(days=i)).isoformat() for i in range(7)]
ROWS = [("z2", 60, 45), ("sweetspot", 75, 90), ("z2", 90, 68), ("rest", 0, 0),
        ("long_z2", 150, 112), ("rest", 0, 0)]


def _plan_dict(budget, today_status="pending", rows=None, days=None):
    days = days or D
    ss = [{"day": days[0], "day_name": "", "session_type": "rest", "duration_min": 0,
           "tss_estimate": 0, "status": today_status, "zwo_file": "", "zwo_name": ""}]
    for i, (st, dur, tss) in enumerate(rows or ROWS, start=1):
        ss.append({"day": days[i], "day_name": "", "session_type": st, "duration_min": dur,
                   "tss_estimate": tss, "status": "pending", "zwo_file": "", "zwo_name": ""})
    return {"goal": {"goal_type": "continuous"},
            "weeks": [{"week_num": 1, "start": days[0], "end": days[-1], "phase": "base",
                       "tss_target": budget, "is_stepback": False, "sessions": ss}]}


def _run(plan, overrides, explicit):
    clock.freeze(TODAY)
    try:
        plan, _n, _i = tp.reforecast_dict(
            plan, tsb_series={TODAY + timedelta(days=i): 0.0 for i in range(-7, 8)},
            availability_overrides=overrides,
            explicit_days=set(overrides) if explicit else None)
    finally:
        clock.unfreeze()
    return plan, {s["day"]: s for s in plan["weeks"][0]["sessions"]}


def _load(by_day):
    return sum(float(s["tss_estimate"] or 0) for s in by_day.values()
               if s["session_type"] != "rest")


def test_an_hour_on_a_rest_day_in_a_full_week_is_a_ride_and_the_week_keeps_its_load():
    _p, by = _run(_plan_dict(315), {D[0]: 1.0}, explicit=True)   # 45+90+68+112 = 315: full
    assert (by[D[0]]["session_type"], by[D[0]]["duration_min"]) == ("z2", 60)
    assert _load(by) <= 315 + 1
    assert (by[D[2]]["duration_min"], by[D[2]]["tss_estimate"]) == (75, 90)   # intensity kept
    assert by[D[5]]["duration_min"] < 150 and by[D[5]]["shortened_from"] == 150
    assert "make room" in by[D[5]]["description"]


def test_a_sync_pass_keeps_hours_a_ceiling():
    """D5 stands for every pass but the rider's own change."""
    _p, by = _run(_plan_dict(315), {D[0]: 1.0}, explicit=False)
    assert by[D[0]]["session_type"] == "rest"
    assert _load(by) == 315 and "shortened_from" not in by[D[5]]


def test_a_day_a_move_vacated_takes_the_hours_and_is_pending_on_disk():
    _p, by = _run(_plan_dict(360, today_status=f"moved_from:{D[3]}"), {D[0]: 1.0}, explicit=True)
    assert (by[D[0]]["session_type"], by[D[0]]["duration_min"], by[D[0]]["status"]) == ("z2", 60, "pending")
    _p, by = _run(_plan_dict(360, today_status=f"moved_from:{D[3]}"), {D[0]: 1.0}, explicit=False)
    assert by[D[0]]["session_type"] == "rest" and by[D[0]]["status"] == f"moved_from:{D[3]}"


def test_room_in_the_week_shortens_nothing():
    _p, by = _run(_plan_dict(400), {D[0]: 1.0}, explicit=True)
    assert by[D[0]]["duration_min"] == 60
    assert [by[d]["duration_min"] for d in D[1:]] == [60, 75, 90, 0, 150, 0]


def test_easy_volume_never_goes_below_the_floor_and_intensity_is_never_cut():
    rows = [("sweetspot", 75, 90), ("threshold", 60, 90), ("z2", 40, 30), ("rest", 0, 0),
            ("vo2max", 60, 75), ("rest", 0, 0)]
    _p, by = _run(_plan_dict(285, rows=rows), {D[0]: 1.0}, explicit=True)
    assert by[D[0]]["duration_min"] == 60
    assert by[D[3]]["duration_min"] == tp._VOLUME_MIN_SESSION_MIN     # cut to the floor only
    assert [by[d]["tss_estimate"] for d in (D[1], D[2], D[5])] == [90, 90, 75]


def test_setting_the_day_back_to_zero_gives_the_week_its_length_back():
    """G2: the shortened sessions get their length back first."""
    plan, by = _run(_plan_dict(315), {D[0]: 1.0}, explicit=True)
    assert by[D[5]]["duration_min"] < 150
    plan, by = _run(plan, {D[0]: 0.0}, explicit=True)
    assert by[D[0]]["session_type"] == "rest"
    assert by[D[5]]["duration_min"] == 150 and "shortened_from" not in by[D[5]]
    assert "make room" not in by[D[5]]["description"] and _load(by) <= 315 + 1


def test_a_day_zeroed_in_the_same_save_makes_the_room():
    """G6: zero Wednesday and ride today in one save — nothing else shrinks."""
    _p, by = _run(_plan_dict(315), {D[0]: 1.0, D[3]: 0.0}, explicit=True)
    assert by[D[0]]["duration_min"] == 60 and by[D[3]]["session_type"] == "rest"
    assert by[D[5]]["duration_min"] == 150 and "shortened_from" not in by[D[5]]


def test_a_friday_to_thursday_week_makes_room_only_ahead_of_today():
    """G7: plan weeks that are not Mon-Sun; days already ridden are not touched."""
    days = [(TODAY + timedelta(days=i)).isoformat() for i in range(-3, 4)]   # Fri..Thu
    rows = [("z2", 60, 45), ("z2", 90, 68), ("long_z2", 150, 112),           # Fri-Sun, past
            ("sweetspot", 75, 90), ("z2", 90, 68), ("long_z2", 120, 90)]     # Tue-Thu
    plan = _plan_dict(473, rows=rows, days=[days[3]] + days[:3] + days[4:])
    plan["weeks"][0]["sessions"].sort(key=lambda s: s["day"])
    plan["weeks"][0].update(start=days[0], end=days[-1])
    for s in plan["weeks"][0]["sessions"][:3]:
        s["status"] = "done"
    _p, by = _run(plan, {D[0]: 1.0}, explicit=True)
    assert (by[D[0]]["session_type"], by[D[0]]["duration_min"]) == ("z2", 60)
    assert [by[d]["duration_min"] for d in days[:3]] == [60, 90, 150]        # past: untouched
    assert by[days[6]]["duration_min"] < 120 and by[days[4]]["duration_min"] == 75


def test_save_availability_passes_the_changed_days(stub):
    """The endpoint is the only explicit caller: a rest day given an hour
    there becomes a ride on disk."""
    ss = [{"day": d, "day_name": "", "session_type": "rest", "duration_min": 0,
           "tss_estimate": 0, "status": "pending", "zwo_file": "", "zwo_name": ""}
          for d in D]
    _write_plan(ss)
    p = Path(tp.PLAN_DIR) / "current_plan.json"
    clock.freeze(TODAY)
    try:
        r = stub.client.post("/api/plan/save-availability",
                             json={"availability": {D[1]: {"hours": 1.0, "type": "available"}}})
    finally:
        clock.unfreeze()
    assert r.status_code == 200, r.text
    day = next(s for w in json.loads(p.read_text())["weeks"] for s in w["sessions"] if s["day"] == D[1])
    assert (day["session_type"], day["duration_min"]) == ("z2", 60)


def test_a_learned_move_then_hours_on_the_vacated_day_keeps_one_event_per_workout(env):
    """G4: Wednesday's workout was dragged to Friday in intervals.icu and
    learned; the rider then gives Wednesday an hour. Friday keeps the moved
    workout, Wednesday gets its own ride, no event is doubled or lost."""
    _seed(env, _hold_week(mon="sweet.zwo", wed="steady.zwo"))
    fri0 = _eid(env, HD["fri"])
    env.fk.drag(_eid(env, HD["wed"]), HD["fri"])
    icp.reconcile()
    assert _on(HD["wed"])[0]["status"] == f"moved_from:{HD['fri']}"
    r = env.client.post("/api/plan/save-availability",
                        json={"availability": {HD["wed"]: {"hours": 1.0, "type": "available"}}})
    assert r.status_code == 200, r.text
    wed, = _on(HD["wed"])
    assert (wed["session_type"], wed["status"]) == ("z2", "pending")
    # the app's plan self-heal gives the restored day a file; here: vo2.zwo
    plan = _plan()
    for w in plan["weeks"]:
        for s in w["sessions"]:
            if s["day"] == HD["wed"]:
                s["zwo_file"] = "vo2.zwo"
    (Path(tp.PLAN_DIR) / "current_plan.json").write_text(json.dumps(plan))
    r = icp.reconcile()
    assert not r.get("error") and r["held"] == 0, r
    assert env.fk.where(fri0) == [HD["fri"]]
    assert env.fk.files_on() == {HD["mon"]: ["sweet"], HD["wed"]: ["vo2"], HD["fri"]: ["steady"]}
    _one_event_per_session(env)


def test_raising_the_hours_on_a_day_that_rides_is_followed_and_the_week_makes_room():
    """IP F (G8, owner): the added ride from 1 h to 2 h is 2 h; a sync pass
    with the same calendar grows nothing (D5)."""
    plan, by = _run(_plan_dict(315), {D[0]: 1.0}, explicit=True)
    plan, by = _run(plan, {D[0]: 2.0}, explicit=True)
    assert (by[D[0]]["session_type"], by[D[0]]["duration_min"]) == ("z2", 120)
    assert _load(by) <= 315 + 1
    assert (by[D[2]]["duration_min"], by[D[2]]["tss_estimate"]) == (75, 90)   # intensity kept
    plan, by = _run(plan, {D[0]: 3.0}, explicit=False)
    assert by[D[0]]["duration_min"] == 120
