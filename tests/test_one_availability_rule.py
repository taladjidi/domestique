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
def test_the_week_total_agrees_with_the_per_day_rule(name):
    goal = GOALS[name]
    assert tp.week_available_minutes(goal, MONDAY) == sum(
        availability.trainable_minutes(goal, (MONDAY + timedelta(days=d)).weekday())
        for d in range(7))


def test_the_week_total_is_a_number_i_can_check_by_hand():
    """The version above is a TAUTOLOGY -- week_available_minutes IS that sum,
    so it passes under every mutation, including one that swaps the weekday and
    weekend defaults. This one is arithmetic a reader can verify."""
    goal = GOALS["defaults only"]          # 1.5 h Mon-Fri, 4.0 h Sat/Sun
    assert tp.week_available_minutes(goal, MONDAY) == 5 * 90 + 2 * 240 == 930


def test_the_documented_defaults_are_the_documented_numbers():
    """Asserting against availability.DEFAULT_* is circular: it cannot fail.
    The rider-visible promise is the NUMBERS."""
    assert availability.DEFAULT_WEEKDAY_HOURS == 2.0
    assert availability.DEFAULT_WEEKEND_HOURS == 3.5
    bare = SimpleNamespace(daily_max_hours={}, max_weekday_hours=None,
                           max_weekend_hours=None, rest_days=[],
                           available_days=list(range(7)))
    assert [availability.day_cap_minutes(bare, wd) for wd in range(7)] == \
        [120, 120, 120, 120, 120, 210, 210]


def test_a_goal_with_no_cap_does_not_explode_or_go_unlimited():
    """Two copies raised TypeError on this goal and a third treated it as
    unlimited. It gets the documented default now, which is a number."""
    goal = GOALS["no cap on record at all"]
    for weekday in range(7):
        assert availability.day_cap_minutes(goal, weekday) > 0


def test_a_rest_day_is_not_a_short_day():
    """The distinction the sampler's copies left to their callers: a cap of
    zero and 'the rider does not train that day' are different answers, and
    only one of them is about the clamp."""
    goal = GOALS["rest and unavailable days"]
    assert availability.day_cap_minutes(goal, 0) == 120      # Monday has a cap
    assert availability.trainable_minutes(goal, 0) == 0      # but it is a rest day
    assert availability.trainable_minutes(goal, 6) == 0      # Sunday is unavailable
    assert availability.trainable_minutes(goal, 1) == 120


def test_an_empty_available_days_means_no_restriction():
    """Not "no day is available". Every legacy reader agreed -- the old
    week_available_minutes wrote ``or range(7)`` -- and reading it the other
    way turned a 900-minute week into a 0-minute one and wrote the rider's
    whole availability calendar as {"hours": 0.0, "type": "rest"}."""
    goal = SimpleNamespace(daily_max_hours={}, max_weekday_hours=2.0,
                           max_weekend_hours=3.0, rest_days=[], available_days=[])
    assert availability.trains_on(goal, 1) is True
    assert tp.week_available_minutes(goal, MONDAY) == 5 * 120 + 2 * 180
    assert week_plan.TrainingWeek._day_cap_min(
        SimpleNamespace(ctx=SimpleNamespace(goal=goal)), MONDAY + timedelta(days=1)) == 120


def test_a_corrupt_per_day_entry_falls_back_like_a_missing_one():
    """It used to fall back to 0.0 here and to the default below, so one bad
    entry silently converted a training day into a rest day."""
    goal = SimpleNamespace(daily_max_hours={1: None, 2: "nonsense"},
                           max_weekday_hours=2.0, max_weekend_hours=3.0,
                           rest_days=[], available_days=list(range(7)))
    assert availability.day_cap_minutes(goal, 1) == 120
    assert availability.day_cap_minutes(goal, 2) == 120
    assert tp.week_available_minutes(goal, MONDAY) == 5 * 120 + 2 * 180


def test_a_negative_cap_is_not_a_negative_limit():
    """-180 flowed into week_plan._commit's min() as a limit."""
    goal = SimpleNamespace(daily_max_hours={0: -1.0}, max_weekday_hours=-3.0,
                           max_weekend_hours=2.0, rest_days=[],
                           available_days=list(range(7)))
    assert availability.day_cap_minutes(goal, 0) == 0
    assert availability.day_cap_minutes(goal, 1) == 0


def test_the_rider_gets_the_minutes_they_asked_for():
    """The dashboard sends hours as minutes/60, so 2h03 arrives as 2.05.
    int() truncated it to 122 and wrote 2.0333 back, losing a minute on every
    regeneration. Ten values between 1 and 720 minutes were affected."""
    goal = SimpleNamespace(daily_max_hours={1: 123 / 60}, max_weekday_hours=2.0,
                           max_weekend_hours=3.5, rest_days=[],
                           available_days=list(range(7)))
    assert availability.day_cap_minutes(goal, 1) == 123
    for minutes in (123, 245, 246, 247, 490, 492, 494, 505, 507, 509):
        g = SimpleNamespace(daily_max_hours={1: minutes / 60},
                            max_weekday_hours=2.0, max_weekend_hours=3.5,
                            rest_days=[], available_days=list(range(7)))
        assert availability.day_cap_minutes(g, 1) == minutes


def test_the_long_ride_ceiling_reads_the_same_rule():
    """Three planner sites read ``(goal.max_weekend_hours or 0) * 60``, which
    ignores a per-day weekend override and hands _apply_long_ride_target a
    ceiling of ZERO for a goal with no weekend default."""
    bare = SimpleNamespace(daily_max_hours={}, max_weekday_hours=None,
                           max_weekend_hours=None, rest_days=[],
                           available_days=list(range(7)))
    assert availability.weekend_cap_minutes(bare) == 210      # not 0
    capped = SimpleNamespace(daily_max_hours={5: 5.0}, max_weekday_hours=2.0,
                             max_weekend_hours=3.5, rest_days=[],
                             available_days=list(range(7)))
    assert availability.weekend_cap_minutes(capped) == 300     # not 210


def test_the_auditor_is_not_blind_where_the_week_view_was():
    """plan_invariants._day_cap_h read ONLY daily_max_hours when the goal had
    no max_hours_for_day method, returning None -- and its consumer SKIPS the
    check on None. The auditor meant to catch an over-cap session was blind
    for exactly the goals the week view was blind for."""
    import plan_invariants as pi
    goal = SimpleNamespace(daily_max_hours={}, max_weekday_hours=2.0,
                           max_weekend_hours=3.5, rest_days=[0],
                           available_days=list(range(7)))
    tuesday = MONDAY + timedelta(days=1)
    cap = pi._day_cap_h(tuesday, goal)
    assert cap is not None, (
        "the daily_duration_cap invariant is skipped for this goal: the "
        "auditor cannot see a 173-minute session against a 120-minute cap")
    assert cap == availability.day_cap_hours(goal, 1) == 2.0


def test_the_real_goal_class_uses_the_shared_rule():
    goal = tp.Goal(goal_type="continuous", target_date=None,
                   daily_max_hours={3: 0.5}, max_weekday_hours=2.0,
                   max_weekend_hours=3.0)
    assert goal.max_hours_for_day(3) == 0.5
    assert goal.max_hours_for_day(1) == 2.0
    assert goal.max_hours_for_day(5) == 3.0
