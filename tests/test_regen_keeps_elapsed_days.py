"""3.13.2 (IP_3.13.2 D) — a rebuild keeps the current week's days before today.

regenerate_from_today kept only the weeks that had ended and rebuilt from
today, so the current week's earlier days fell out of the plan with the
rides done there (owner's plan: 10-12 to 10-15 in no week after the 10-16
rebuild). They are now kept as a week of their own ending yesterday; the
stepback cadence still counts that calendar week once, the load guard does
not read a remainder too short to judge, and the absence latch keys on days
so the kept remainder does not set off a second rebuild.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

import app
import clock
import training_planner as tp

_LIB_INDEX = Path(__file__).resolve().parent.parent / "src" / "workouts" / ".library_index.json"
MON = date(2026, 9, 7)
THU = MON + timedelta(days=17)          # Thursday of the third week
TYPES = ("z2", "z2", "rest", "threshold", "z2", "long_z2", "rest")


def _week(n, start, target=300, status="pending"):
    ss = []
    for i, t in enumerate(TYPES):
        d = start + timedelta(days=i)
        ss.append({"day": d.isoformat(), "day_name": d.strftime("%a"), "session_type": t,
                   "duration_min": 0 if t == "rest" else 60, "tss_estimate": 0 if t == "rest" else 50,
                   "status": status if t != "rest" else "pending", "description": "",
                   "zwo_file": "", "zwo_name": ""})
    return {"week_num": n, "start": start.isoformat(), "end": (start + timedelta(days=6)).isoformat(),
            "phase": "build", "tss_target": target, "is_stepback": False, "sessions": ss}


def _rides(start, days):
    return [{"date": (start + timedelta(days=i)).isoformat(), "tss": 50} for i in range(days)]


@pytest.fixture()
def _keep_library_index():
    backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None
    yield
    if backup is not None:
        _LIB_INDEX.write_bytes(backup)


def _regen(weeks, acts):
    clock.freeze(THU)
    try:
        goal = tp.Goal(goal_type="general", target_date=THU + timedelta(weeks=12), hours_per_week=8.0)
        return tp.regenerate_from_today(goal, [tp.week_from_dict(w) for w in weeks], 50.0,
                                        activities=acts, seed_salt=1, recent_weekly_tss=300.0)[1]
    finally:
        clock.unfreeze()


def test_a_mid_week_rebuild_keeps_the_days_before_today(_keep_library_index):
    cur = _week(3, MON + timedelta(days=14))
    cur["sessions"][0].update(status="done", completion_matches=[{"activity_id": "i1"}])
    weeks = _regen([_week(1, MON), _week(2, MON + timedelta(days=7)), cur, _week(4, MON + timedelta(days=21))],
                   _rides(MON, 17))
    kept = next(w for w in weeks if w.end == THU - timedelta(days=1))
    assert kept.start == MON + timedelta(days=14) and kept.week_num == 3
    assert [s.day for s in kept.sessions] == [MON + timedelta(days=14 + i) for i in range(3)]
    assert kept.sessions[0].status == "done" and kept.sessions[0].completion_matches
    assert kept.tss_target == 100                       # the planned load of those days
    days = [s.day for w in weeks for s in w.sessions]
    assert len(days) == len(set(days))                  # no day twice
    assert sorted(days) == [MON + timedelta(days=i) for i in range(len(days))]   # none missing
    assert [w.week_num for w in weeks] == list(range(1, len(weeks) + 1))


def test_the_kept_days_do_not_bring_the_stepback_forward(_keep_library_index):
    """GD1: two load weeks + this calendar week = three; the stepback is next week."""
    weeks = _regen([_week(1, MON), _week(2, MON + timedelta(days=7)),
                    _week(3, MON + timedelta(days=14)), _week(4, MON + timedelta(days=21))],
                   _rides(MON, 17))
    row = next(w for w in weeks if w.start == THU)
    nxt = next(w for w in weeks if w.start == THU + timedelta(days=4))
    assert (row.is_stepback, nxt.is_stepback) == (False, True)


def test_the_load_guard_does_not_read_a_remainder_too_short_to_judge():
    """GD3: a one-day remainder with a ride on a rest day read as ratio 50."""
    weeks = [tp.week_from_dict(_week(1, MON)), tp.week_from_dict(_week(2, MON + timedelta(days=7)))]
    weeks[1].end = weeks[1].start                        # a one-day remainder, target 0
    weeks[1].sessions = weeks[1].sessions[:1]
    weeks[1].tss_target = 0
    clock.freeze(MON + timedelta(days=8))
    try:
        r = tp._last_completed_week_acwr(weeks, _rides(MON, 8))
    finally:
        clock.unfreeze()
    assert r == pytest.approx(350 / 300)


def test_an_absence_rebuild_is_not_repeated_once_the_week_is_kept(_keep_library_index):
    """GD4: two missed weeks and four missed days rebuild once; the next day,
    with those four days kept as a finished week, the latch still holds."""
    w0 = THU - timedelta(days=18)
    plan = {"goal": {"type": "general", "hours_per_week": 8, "max_weekday_hours": 2.0,
                     "max_weekend_hours": 3.5, "rest_days": [2, 6], "longest_ride_h_90d": 2.0},
            "availability": {},
            "weeks": [_week(1, w0), _week(2, w0 + timedelta(days=7)), _week(3, w0 + timedelta(days=14)),
                      _week(4, w0 + timedelta(days=21)), _week(5, w0 + timedelta(days=28))]}
    for day, label in ((THU, "rebuilt"), (THU + timedelta(days=1), "rebalanced")):
        clock.freeze(day)
        try:
            plan, action, _i, status = app._apply_plan_update(
                plan, training={"ctl": 30, "tsb": 10}, activities=[], today=day,
                allow_regen=True, gap_debounce=True)
        finally:
            clock.unfreeze()
        assert action == label, status


def test_a_split_week_counts_once_at_every_later_rebuild():
    """GD1 again: once saved, the kept part and the rebuilt rest are two rows
    of one calendar week; recalc, extend and the next rebuild count rows."""
    def row(n, start, days):
        w = tp.week_from_dict(_week(n, start))
        w.end = start + timedelta(days=days - 1)
        w.sessions = w.sessions[:days]
        return w
    w2 = MON + timedelta(days=7)
    split = [row(1, MON, 7), row(2, w2, 3), row(3, w2 + timedelta(days=3), 4)]
    clock.freeze(MON + timedelta(days=21))
    try:
        assert tp._calendar_weeks(split) == 2
        assert not tp.stepback_due(split, "build", MON + timedelta(days=20))
        assert tp.stepback_due([row(0, MON - timedelta(days=7), 7), *split], "build",
                               MON + timedelta(days=20))
    finally:
        clock.unfreeze()
