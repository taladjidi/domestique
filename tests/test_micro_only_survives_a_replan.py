"""A rider preference holds on every path that rebuilds sessions.

"VO2max microintervals only" was honoured in generate, recalculate and
extend, and not in regenerate -- the path a rider takes most, because it is
what the Replan button calls. So the preference survived until the first
replan and then quietly stopped holding, which is the worst shape for a
setting: it works when you check it and not when you use it.

Matching alone does not carry it either. `match_zwo(..., micro_only=True)`
picks a micro file when it is asked to pick one, but the sampler and the
volume floors place sessions of their own, so the guarantee has to be a pass
over the built weeks. That is `_enforce_microintervals_only`, and this file
asserts every rebuilding entry point runs it.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import clock                                          # noqa: E402
import training_planner as tp                         # noqa: E402

TODAY = date(2026, 9, 17)


def _goal(**kw):
    g = tp.Goal(goal_type="ftp", target_date=TODAY + timedelta(weeks=10))
    g.vo2_microintervals_only = True
    g.hours_per_week = 8.0
    for k, v in kw.items():
        setattr(g, k, v)
    return g


def _long_rep_vo2(weeks) -> list:
    """VO2max days whose served file is not a microinterval workout.

    Only vo2max: the preference is about how the VO2 work is delivered, not
    about threshold or sweet spot, which have no micro form. The pass reads
    the same field (`IsMicro`) from the same library rows.
    """
    facts = {r["File"]: r for r in tp.load_workout_library()}
    out = []
    for w in weeks:
        for s in getattr(w, "sessions", []):
            if getattr(s, "session_type", "") != "vo2max":
                continue
            zwo = getattr(s, "zwo_file", "") or ""
            row = facts.get(zwo) if zwo else None
            if row is not None and not row.get("IsMicro"):
                out.append((getattr(s, "day", None), zwo))
    return out


@pytest.fixture(autouse=True)
def _frozen():
    clock.freeze(TODAY)
    yield
    clock.unfreeze()


def test_every_rebuilding_entry_point_runs_the_preference_pass():
    """The structural assertion, and the one that survives a refactor: the
    pass chain is hand-assembled per entry point, so an entry point added
    later inherits nothing."""
    import inspect
    missing = []
    for name in ("generate_plan", "regenerate_from_today", "recalculate_plan",
                 "extend_continuous_plan"):
        src = inspect.getsource(getattr(tp, name))
        if "_enforce_microintervals_only" not in src:
            missing.append(name)
    assert missing == [], (
        f"{missing} rebuild sessions without applying the rider's "
        f"microintervals-only preference; match_zwo's flag does not cover the "
        f"sessions the sampler and the floors place")


@pytest.mark.slow
def test_a_replan_serves_no_long_rep_session_to_a_micro_only_rider():
    """The behaviour, end to end, on the path the Replan button calls."""
    goal = _goal()
    phases, weeks = tp.generate_plan(goal, current_ctl=45.0, recent_weekly_tss=350.0)
    assert _long_rep_vo2(weeks) == [], "generate already leaked"

    _phases, all_weeks, _info = tp.regenerate_from_today(
        goal, weeks, current_ctl=45.0, activities=[], seed_salt=7)
    leaked = _long_rep_vo2([w for w in all_weeks if w.end >= TODAY])
    assert leaked == [], (
        f"the replan served {len(leaked)} long-rep session(s) to a rider who "
        f"asked for microintervals only: {leaked[:3]}")
