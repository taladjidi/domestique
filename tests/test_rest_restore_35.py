"""#35 — the availability pass never restores a rest day as a few-minute ride.

A week a few TSS under its budget, and a rest day with free hours: the
rest-restore branch trimmed the restored z2 to what the budget still held and
kept anything above zero, so the day became a 1- to 6-minute "restored" ride,
which a later rematch filled with a 240-minute file that went to the watch.
Below the planner's own session floor (_VOLUME_MIN_SESSION_MIN, 30 min) the
day now stays rest. A ride that is restored is the planner's, so it carries no
pins left over from a session the rider once moved or swapped onto that day.
"""
from __future__ import annotations

from datetime import date, timedelta

import clock
import training_planner as tp

TODAY = date(2026, 9, 15)          # a Tuesday
MONDAY = TODAY - timedelta(days=1)
WED = (MONDAY + timedelta(days=2)).isoformat()


def _week(tss_target: float, wed_extra: dict | None = None):
    sessions = [
        {"day": MONDAY.isoformat(), "day_name": "Mon", "session_type": "sweetspot",
         "duration_min": 79, "tss_estimate": 105, "status": "pending", "zwo_file": ""},
        {"day": TODAY.isoformat(), "day_name": "Tue", "session_type": "z2",
         "duration_min": 73, "tss_estimate": 55, "status": "pending", "zwo_file": ""},
    ] + [{"day": (MONDAY + timedelta(days=i)).isoformat(), "day_name": "",
          "session_type": "rest", "duration_min": 0, "tss_estimate": 0,
          "status": "pending", "zwo_file": ""} for i in range(2, 7)]
    if wed_extra:
        sessions[2].update(wed_extra)
    return tp.week_from_dict({
        "week_num": 1, "start": MONDAY.isoformat(),
        "end": (MONDAY + timedelta(days=6)).isoformat(), "phase": "base",
        "tss_target": tss_target, "is_stepback": False, "sessions": sessions})


def _reforecast(week):
    clock.freeze(TODAY)
    try:
        tp.reforecast(tp.Goal(goal_type="continuous"), [week],
                      tsb_series={MONDAY + timedelta(days=i): 0.0 for i in range(7)},
                      availability_overrides={WED: 3.0})
    finally:
        clock.unfreeze()
    return next(s for s in week.sessions if s.day.isoformat() == WED)


def test_a_few_tss_of_room_leaves_the_rest_day_rest():
    # planned 160, budget 165: 5 TSS of room is about 6 minutes of z2
    wed = _reforecast(_week(165))
    assert wed.session_type == "rest", wed.description
    assert wed.duration_min == 0


def test_room_for_a_real_ride_still_restores_it():
    # planned 160, budget 205: 45 TSS of room is an hour of z2
    wed = _reforecast(_week(205))
    assert wed.session_type == "z2"
    assert wed.duration_min >= tp._VOLUME_MIN_SESSION_MIN


def test_a_restored_ride_carries_no_old_pins():
    wed = _reforecast(_week(205, {"user_moved": True, "user_swapped": True,
                                  "moved_from": (MONDAY + timedelta(days=5)).isoformat()}))
    assert wed.session_type == "z2"
    assert (wed.user_moved, wed.user_swapped, wed.moved_from) == (False, False, "")


def test_a_restored_ride_drops_marks_of_the_session_it_replaced():
    """A stale pre_adapt would let revert-cap restore the old session over the
    restored ride; adapted/auto_moved/tsb_eased_from describe that old one."""
    week = _week(205, {"adapted": True, "auto_moved": True, "adapted_reason": "tier-down",
                       "pre_adapt": {"session_type": "vo2max", "duration_min": 60},
                       "tsb_eased_from": 90})
    wed = _reforecast(week)
    assert wed.session_type == "z2" and wed.adapted is False
    stored = tp.session_to_dict(wed)
    for key in ("auto_moved", "adapted_reason", "pre_adapt", "tsb_eased_from"):
        assert key not in stored, key
