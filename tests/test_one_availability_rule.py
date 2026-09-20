"""Everyone who caps a day caps it the same way.

The rule -- a weekday's own cap if the rider set one, else the weekday or
weekend default, and nothing on a rest or unavailable day -- was written out
five times, and three of the copies disagreed:

  * `week_plan.TrainingWeek._day_cap_min` returned None for "no cap set",
    and None meant NO LIMIT there while the sampler's copy treated the same
    goal as zero minutes. The week view failed OPEN exactly where the planner
    failed CLOSED, and an audit reproduced a 173-minute session against a
    120-minute cap.
  * two copies raised TypeError on `int(None * 60)` for a goal with no
    default at all.
  * the sampler's copies left rest days to the caller while app's copy and
    `week_available_minutes` returned zero for them, so "Monday's cap" meant
    two different things depending on who asked.

They now all read src/availability.py. These tests compare the readers against
each other on goals chosen to hit the corners, which is the check none of the
five copies could fail.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import availability                                   # noqa: E402
import training_planner as tp                         # noqa: E402
import week_plan                                      # noqa: E402

MONDAY = date(2026, 9, 14)

GOALS = {
    "per-day caps": SimpleNamespace(
        daily_max_hours={0: 1.0, 1: 1.5, 2: 2.0, 3: 1.0, 4: 2.5, 5: 4.0, 6: 3.0},
        max_weekday_hours=2.0, max_weekend_hours=3.5,
        rest_days=[], available_days=list(range(7))),
    "defaults only": SimpleNamespace(
        daily_max_hours={}, max_weekday_hours=1.5, max_weekend_hours=4.0,
        rest_days=[], available_days=list(range(7))),
    "rest and unavailable days": SimpleNamespace(
        daily_max_hours={}, max_weekday_hours=2.0, max_weekend_hours=3.0,
        rest_days=[0], available_days=[1, 2, 3, 4, 5]),
    "no cap on record at all": SimpleNamespace(
        daily_max_hours={}, max_weekday_hours=None, max_weekend_hours=None,
        rest_days=[], available_days=list(range(7))),
    "partial per-day caps": SimpleNamespace(
        daily_max_hours={2: 0.75}, max_weekday_hours=2.0, max_weekend_hours=3.0,
        rest_days=[6], available_days=[0, 1, 2, 3, 4, 5]),
}


@pytest.mark.parametrize("name", sorted(GOALS))
@pytest.mark.parametrize("weekday", range(7))
def test_every_reader_gives_the_same_cap(name, weekday):
    goal = GOALS[name]
    shared = availability.day_cap_minutes(goal, weekday)

    # the week view's reader, which used to answer None here
    view = week_plan.TrainingWeek._day_cap_min(
        SimpleNamespace(ctx=SimpleNamespace(goal=goal)),
        MONDAY + timedelta(days=weekday))
    assert view is not None, (
        f"{name}, weekday {weekday}: the week view says 'no cap' while the "
        f"planner says {shared} minutes. That gap is how a 173-minute session "
        f"got past a 120-minute cap")
    assert float(view) == float(shared), f"{name}, weekday {weekday}"

    # the Goal method the sampler's clamps call
    if hasattr(goal, "max_hours_for_day"):
        assert int(goal.max_hours_for_day(weekday) * 60) == shared


@pytest.mark.parametrize("name", sorted(GOALS))
def test_the_week_total_is_the_sum_of_its_days(name):
    """`week_available_minutes` and the per-day rule cannot drift: one is
    defined as the sum of the other."""
    goal = GOALS[name]
    assert tp.week_available_minutes(goal, MONDAY) == sum(
        availability.trainable_minutes(goal, (MONDAY + timedelta(days=d)).weekday())
        for d in range(7))


def test_a_goal_with_no_cap_does_not_explode_or_go_unlimited():
    """Two copies raised TypeError on this goal and a third treated it as
    unlimited. It gets the documented default now, which is a number."""
    goal = GOALS["no cap on record at all"]
    for weekday in range(7):
        minutes = availability.day_cap_minutes(goal, weekday)
        assert minutes > 0
        expected = (availability.DEFAULT_WEEKEND_HOURS if weekday >= 5
                    else availability.DEFAULT_WEEKDAY_HOURS)
        assert minutes == int(expected * 60)


def test_a_rest_day_is_not_a_short_day():
    """The distinction the sampler's copies left to their callers: a cap of
    zero and 'the rider does not train that day' are different answers, and
    only one of them is about the clamp."""
    goal = GOALS["rest and unavailable days"]
    assert availability.day_cap_minutes(goal, 0) == 120      # Monday has a cap
    assert availability.trainable_minutes(goal, 0) == 0      # but it is a rest day
    assert availability.trainable_minutes(goal, 6) == 0      # Sunday is unavailable
    assert availability.trainable_minutes(goal, 1) == 120


def test_the_real_goal_class_uses_the_shared_rule():
    goal = tp.Goal(goal_type="continuous", target_date=None,
                   daily_max_hours={3: 0.5}, max_weekday_hours=2.0,
                   max_weekend_hours=3.0)
    assert goal.max_hours_for_day(3) == 0.5
    assert goal.max_hours_for_day(1) == 2.0
    assert goal.max_hours_for_day(5) == 3.0
