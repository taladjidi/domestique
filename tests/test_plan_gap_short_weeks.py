"""3.13.2 — a week too short to judge is not a missed week.

Owner report: a continuous plan restarted on Sunday 2026-10-11 rebuilt
itself on 10-16 with a recovery ramp (a reconditioning week and two ramp
weeks) although the rider had been training. The plan's first week ran
from Sunday to Sunday: one day. Not ridden, it counted as a missed week and
as 7 days of absence, and every rebuild adds a ramp from 7 days on.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

import clock
import training_planner as tp

_LIB_INDEX = Path(__file__).resolve().parent.parent / "src" / "workouts" / ".library_index.json"
SUN = date(2026, 10, 11)


def _w(n, start, days, target):
    return tp.PlannedWeek(week_num=n, start=start, end=start + timedelta(days=days - 1),
                          phase="continuous", tss_target=target, is_stepback=False, sessions=[])


def _gaps(weeks, acts, today):
    clock.freeze(today)
    try:
        return tp.detect_plan_gaps(weeks, acts, 22.0)
    finally:
        clock.unfreeze()


def _owner_plan():
    return [_w(1, SUN, 1, 28), _w(2, SUN + timedelta(days=1), 7, 199),
            _w(3, SUN + timedelta(days=8), 7, 223)]


@pytest.fixture()
def _keep_library_index():
    backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None
    yield
    if backup is not None:
        _LIB_INDEX.write_bytes(backup)


def test_a_plan_made_on_sunday_has_a_one_day_first_week_that_is_not_judged(_keep_library_index):
    clock.freeze(SUN)
    try:
        g = tp.Goal(goal_type="continuous", target_date=None, hours_per_week=6.0, focus="both")
        out = tp.generate_plan(g, current_ctl=22.0, recent_weekly_tss=150)
    finally:
        clock.unfreeze()
    weeks = out[1] if isinstance(out, tuple) else out
    assert (weeks[0].start, weeks[0].end) == (SUN, SUN)
    assert not tp.gap_judged_week(weeks[0]) and tp.gap_judged_week(weeks[1])


@pytest.mark.parametrize("acts", [[], [{"date": "2026-10-13", "tss": 80},
                                       {"date": "2026-10-15", "tss": 70}]])
def test_the_unridden_first_day_builds_no_recovery_ramp(acts):
    g = _gaps(_owner_plan(), acts, SUN + timedelta(days=5))
    assert g["needs_regeneration"] is False and g["absence_days"] < 7
    assert all(w["week_num"] != 1 for w in g["gap_weeks"])
    goal = tp.Goal(goal_type="continuous", target_date=None, hours_per_week=6.0)
    assert tp.build_recovery_ramp(22.0, g["absence_days"], goal) == []


def test_a_streak_carries_across_a_stub_and_counts_real_days():
    mon = date(2026, 9, 7)
    weeks = [_w(1, mon, 7, 200), _w(2, mon + timedelta(days=7), 3, 90),
             _w(3, mon + timedelta(days=10), 7, 200)]
    g = _gaps(weeks, [{"date": (mon + timedelta(days=8)).isoformat(), "tss": 90}],
              mon + timedelta(days=20))            # the stub was ridden in full
    assert (g["consecutive_missed"], g["absence_days"], g["needs_regeneration"]) == (2, 14, True)


def test_full_weeks_count_as_before():
    mon = date(2026, 9, 7)
    weeks = [_w(1, mon, 7, 200), _w(2, mon + timedelta(days=7), 7, 200)]
    g = _gaps(weeks, [], mon + timedelta(days=11))  # week 1 missed, 4 days of week 2
    assert (g["consecutive_missed"], g["absence_days"]) == (2, 11)


def test_a_short_current_week_is_pro_rata_to_its_own_length():
    tue = date(2026, 9, 8)
    weeks = [_w(1, tue, 6, 120)]                    # Tue-Sun
    g = _gaps(weeks, [{"date": "2026-09-09", "tss": 39}], tue + timedelta(days=4))
    assert g["gap_weeks"] and g["gap_weeks"][0]["planned_tss"] == 80


def test_the_absence_episode_skips_a_completed_stub():
    """GB2: a stub completing after two missed weeks must not end the episode."""
    import app as app_module
    mon = date(2026, 9, 7)
    weeks = [_w(1, mon, 7, 200), _w(2, mon + timedelta(days=7), 7, 200),
             _w(3, mon + timedelta(days=14), 3, 90), _w(4, mon + timedelta(days=17), 7, 200)]
    today = mon + timedelta(days=18)
    g = _gaps(weeks, [], today)
    assert g["consecutive_missed"] >= 2
    assert app_module._current_absence_episode(weeks, g, today) == (1, 2)


def _regen_info(weeks, acts, today):
    clock.freeze(today)
    try:
        goal = tp.Goal(goal_type="general", target_date=today + timedelta(weeks=12), hours_per_week=8.0)
        return tp.regenerate_from_today(goal, weeks, 40.0, activities=acts, seed_salt=1,
                                        recent_weekly_tss=280.0)[2]
    finally:
        clock.unfreeze()


def test_a_rebuild_after_a_recovered_gap_adds_no_ramp(_keep_library_index):
    """IP E: the ramp is sized from the absence still going, not the longest ever."""
    mon = date(2026, 8, 3)
    weeks = [_w(n, mon + timedelta(days=7 * (n - 1)), 7, 280) for n in range(1, 7)]
    ridden = [{"date": (mon + timedelta(days=d)).isoformat(), "tss": 40} for d in range(14, 42)]
    today = mon + timedelta(days=36)
    g = _gaps(weeks, ridden, today)
    assert (g["absence_days"], g["current_absence_days"]) == (14, 0)
    assert _regen_info(weeks, ridden, today)["recovery_ramp_weeks"] == 0
