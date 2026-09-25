"""Target-delivery contract (notes/target-delivery-contract.md), round 9.

One section per assertion. Each test was seen to fail on d190fa82 before its
fix landed; the commit that fixed it records the mutation.
"""
from __future__ import annotations

import copy
import datetime as _dt
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import clock  # noqa: E402
import training_planner as tp  # noqa: E402

import test_planner_finalize as F  # noqa: E402  (the owner and the entry-point drivers)


@pytest.fixture(autouse=True)
def _clock():
    yield
    clock.unfreeze()


@pytest.fixture
def gate_seed(monkeypatch):
    import config
    monkeypatch.setattr(config, "ICU_ATHLETE_ID", "gate-athlete")    # the sweep's seed


def sweep_goal(kind, t, *, rest, daily=None, hpw=8.0, weeks=0, race_in=None, **kw):
    """A rider of the delivery sweep (zz_probe/sweep_gen.py), as a goal."""
    td = None
    if race_in is not None:
        d = t + _dt.timedelta(weeks=race_in)
        td = d + _dt.timedelta(days=(6 - d.weekday()) % 7)
        if kind == "event":
            weeks = max(4, -(-(td - t).days // 7))
    return tp.Goal(goal_type=kind, target_date=td, rest_days=list(rest),
                   available_days=[d for d in range(7) if d not in rest],
                   daily_max_hours=dict(daily or {}), max_weekday_hours=2.0,
                   max_weekend_hours=3.5, hours_per_week=hpw, plan_weeks=weeks,
                   event_km=(160 if kind == "event" else 0),
                   event_climb_m=(2000 if kind == "event" else 0), **kw)


def sweep_gen(goal, t, ctl, rwt, ftp=247):
    clock.freeze(t)
    return tp.generate_plan(goal, seed_salt=t.toordinal(), current_ctl=ctl,
                            recent_weekly_tss=rwt, athlete={"ftp": ftp, "weight_kg": 72})[1]


# ── 9.3 Weekly hours are a ceiling ───────────────────────────────────────

def over_hours(weeks, goal, t):
    """Rows whose minutes from ``t`` on exceed the rider's weekly hours over
    the row's remaining days."""
    out = []
    for w in weeks:
        if w.end < t:
            continue
        room = goal.hours_per_week * 60 * ((w.end - max(w.start, t)).days + 1) / 7
        mins = sum(s.duration_min or 0 for s in w.sessions
                   if s.day >= t and s.session_type != "rest" and not s.is_race)
        if mins > room + 1:
            out.append((str(w.start), mins, round(room)))
    return out


@pytest.mark.parametrize("case", ["event-fixedcore", "ftpvo2-threshold"])
def test_9_3_generate_holds_the_weekly_hours(case, gate_seed):
    """The two sweep riders whose weeks ran over their hours on d190fa82:
    event-fixedcore's 09-21 week 627 min against 600, ftpvo2-threshold's
    opening week 616 against 480."""
    t = _dt.date(2026, 9, 14)
    g, ctl, rwt, ftp = {
        "event-fixedcore": (sweep_goal("event", t, rest=[0], hpw=10, race_in=10,
                                       plan_mode="fixed_core"), 50, 380, 247),
        "ftpvo2-threshold": (sweep_goal("ftp_vo2max", t, rest=[0], hpw=8, weeks=12,
                                        distribution="threshold"), 50, 380, 50)}[case]
    assert not over_hours(sweep_gen(g, t, ctl, rwt, ftp), g, t)


@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_9_3_every_entry_point_holds_the_weekly_hours(kind):
    """The owner (12.5 h a week, 13.5 h of days) after every entry point."""
    base = F.gen(F.owner(kind))
    assert not over_hours(base, F.owner(kind), F.G0)
    for name, weeks in F.entry_points(kind, base, F.T3, lambda: F.owner(kind)):
        assert not over_hours(weeks, F.owner(kind), F.T3), name
