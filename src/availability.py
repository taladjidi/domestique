"""How much the rider can train on a given day. One implementation.

THE RULE. A weekday gets its own cap when the rider set one
(`goal.daily_max_hours`), otherwise the weekday or weekend default. A rest day
and a day the rider is not available get nothing.

WHY IT IS HERE. The rule existed five times: `Goal.max_hours_for_day`,
`week_available_minutes`, the sampler's `_cap_min` and `_max_min_for`,
`week_plan.TrainingWeek._day_cap_min` and app's `_default_hours_for_weekday`.
Three of them disagreed:

  * `week_plan._day_cap_min` returned None, meaning NO CAP, where the others
    returned a number. A missing default made the week view fail OPEN while
    the sampler failed CLOSED, and an audit reproduced a 173-minute session
    against a 120-minute cap.
  * the sampler's two copies treated rest days as the caller's problem, while
    `week_available_minutes` and app's copy returned zero for them, so "the
    cap for Monday" meant two different things depending on who asked.
  * a goal with no default at all raised TypeError in two of the copies
    (`int(None * 60)`) and returned 0 in a third.

`week_available_minutes` even says in its own docstring that duplicating the
rule would let the clamp and the slots disagree, directly above a duplicate of
the rule.

This module is a LEAF on purpose: training_planner and week_plan import each
other already (that pair is in the one remaining import cycle), so the shared
rule cannot live in either of them without deepening it.

Goals are read through getattr because the planner is handed real `Goal`
dataclasses, test doubles and SimpleNamespaces, and the rule has to be the
same for all of them.
"""
from __future__ import annotations

from datetime import date, timedelta

# What a rider can do on a day nobody has said anything about. The Goal
# dataclass carries these defaults itself; they are repeated here for the
# goal-shaped objects that do not.
DEFAULT_WEEKDAY_HOURS = 2.0
DEFAULT_WEEKEND_HOURS = 3.5


def _hours(value, fallback: float) -> float:
    """A number, or the fallback when the goal says nothing usable.

    Never None: a None cap used to mean "no limit" in one reader and "no
    training" in another, which is the disagreement this module exists to end.
    """
    try:
        if value is None:
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


def day_cap_hours(goal, weekday: int) -> float:
    """The most the rider will train on ``weekday`` (0=Mon..6=Sun).

    The cap alone: it does not ask whether the rider trains that day at all.
    Callers that need that ask trainable_minutes().

    A malformed per-day entry falls back to the SAME default as a missing one.
    The first version fell back to 0.0 here and to 2.0/3.5 below, so a single
    corrupt entry turned a training day into a rest day -- in the one module
    whose stated purpose is that a missing cap is never read as "no training".
    """
    if weekday >= 5:
        default = _hours(getattr(goal, "max_weekend_hours", None),
                         DEFAULT_WEEKEND_HOURS)
    else:
        default = _hours(getattr(goal, "max_weekday_hours", None),
                         DEFAULT_WEEKDAY_HOURS)
    per_day = getattr(goal, "daily_max_hours", None) or {}
    hours = _hours(per_day[weekday], default) if weekday in per_day else default
    # A negative cap is not a shorter day; it is nonsense that flows into
    # week_plan._commit's min() as a negative limit. Clamp it here, once.
    return max(0.0, hours)


def day_cap_minutes(goal, weekday: int) -> int:
    # round, not truncate: the dashboard sends hours as minutes/60, so a
    # rider's 2h03 arrives as 2.05 and int() handed it back as 122, then wrote
    # 2.0333 back into the plan -- a minute lost on every regeneration.
    return int(round(day_cap_hours(goal, weekday) * 60))


def weekend_cap_hours(goal) -> float:
    """The longest a weekend day may be -- the ceiling on the long ride.

    Three sites in the planner read ``(goal.max_weekend_hours or 0)`` for this,
    which ignores a per-day Saturday/Sunday override and, on ``or 0``, hands
    _apply_long_ride_target a ceiling of ZERO minutes for a goal carrying no
    weekend default.
    """
    return max(day_cap_hours(goal, 5), day_cap_hours(goal, 6))


def weekend_cap_minutes(goal) -> int:
    return int(round(weekend_cap_hours(goal) * 60))


def trains_on(goal, weekday: int) -> bool:
    """Does the rider train on this weekday at all?

    A rest day is not a small day, and neither is a day outside the rider's
    available days.

    An EMPTY available_days means "no restriction", not "no day available".
    Every legacy reader agreed on that -- week_available_minutes wrote
    ``or range(7)``, plan_invariants ``not avail or wd in avail``, week_plan
    ``goal.available_days and wd not in ...`` -- and the first version of this
    module flipped it, turning a 900-minute week into a 0-minute one.
    """
    if weekday in set(getattr(goal, "rest_days", None) or []):
        return False
    available = getattr(goal, "available_days", None)
    if not available:
        return True
    return weekday in set(available)


def trainable_minutes(goal, weekday: int) -> int:
    """The cap in minutes, or 0 on a day the rider does not train."""
    if not trains_on(goal, weekday):
        return 0
    return day_cap_minutes(goal, weekday)


def week_trainable_minutes(goal, week_start: date) -> int:
    """What the rider can train across the week starting ``week_start``."""
    return sum(trainable_minutes(goal, (week_start + timedelta(days=off)).weekday())
               for off in range(7))
