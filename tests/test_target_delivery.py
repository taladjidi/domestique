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


# ── 9.5 The ramp's availability cap uses the served mix's rate ───────────

def _served_rate(classes):
    """Median TSS an hour of the library files a slot of these classes is
    served (score at or above the class floor), read from the files."""
    import statistics
    r = [float(x["TSS"]) / float(x["Duration(min)"]) * 60 for x in tp.load_workout_library()
         if tp._content_class_for_row(x) in classes
         and float(x.get("Score") or 0) >= tp._class_aware_score_floor(tp._content_class_for_row(x))
         and float(x.get("Duration(min)") or 0) > 0 and float(x.get("TSS") or 0) > 0]
    return statistics.median(r)


def test_9_5_an_hour_is_budgeted_at_what_the_served_files_carry():
    """A five-hour rider who carries 380 TSS a week: every week's budget is
    capped by the hours, and an hour of the phase's mix at the files served
    holds 42-49 TSS (the research's 47.4 for the base mix), not 65 -- a
    target of 325 in five hours was 30 % more than any week could hold."""
    t = _dt.date(2026, 9, 14)
    g = sweep_goal("general", t, rest=[5, 6], daily={d: 1.0 for d in range(5)}, hpw=5, weeks=8)
    weeks = sweep_gen(g, t, 55, 380)
    easy, hard = _served_rate({"endurance"}), _served_rate({"threshold", "vo2max"})
    assert 38 < easy < 48 and hard > 65, (easy, hard)
    full = [w for w in weeks if (w.end - w.start).days == 6 and not w.is_stepback
            and w.phase in ("base", "build1", "build2", "peak")]
    assert full
    for w in full:
        # all-easy is the floor of what five hours hold; a quarter of them
        # hard the ceiling (a short week's doses take a larger share of it)
        assert 5 * easy <= w.tss_target <= 5 * (0.75 * easy + 0.25 * hard), (w.start, w.tss_target)
