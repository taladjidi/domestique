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
    """Rows ahead whose minutes -- the days already behind the rider as they
    were given, the rest as planned -- exceed the rider's weekly hours over
    the row's days (a stub row gets its share)."""
    out = []
    for w in weeks:
        if w.end < t:
            continue
        room = goal.hours_per_week * 60 * ((w.end - w.start).days + 1) / 7
        mins = sum(s.duration_min or 0 for s in w.sessions
                   if s.session_type != "rest" and not s.is_race
                   and s.status not in ("missed", "dismissed"))
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


# ── 9.1 / 9.2 A load week carries its budget, and the ramp sees it ───────

import plan_invariants as pi  # noqa: E402


def load_weeks(weeks, goal, t):
    """Full load rows ahead of ``t``: not unload, not taper, no race."""
    return [w for w in weeks
            if w.start >= t and (w.end - w.start).days >= 6 and not tp._is_unload_week(w)
            and str(w.phase).lower() != "taper" and not any(s.is_race for s in w.sessions)
            and not tp._race_in_week(w, goal)]


def week_load(w, t):
    return sum(float(s.tss_estimate or 0) for s in w.sessions
               if s.day >= t and s.session_type != "rest" and not s.is_race)


def out_of_room(w, goal, t):
    """Nothing left to grow: every pending easy ride at its date's minutes,
    or the week's minutes at the rider's hours."""
    mins = sum(s.duration_min or 0 for s in w.sessions
               if s.day >= t and s.session_type != "rest" and not s.is_race)
    if mins >= goal.hours_per_week * 60 * ((w.end - max(w.start, t)).days + 1) / 7 - 10:
        return True
    return all(s.duration_min >= tp._date_cap_min(goal, s.day, {})[0] - 10
               for s in w.sessions if s.day >= t and s.session_type in ("z2", "long_z2")
               and not s.adapted and not s.is_opener)


def guarded(weeks, t, chronic):
    """start -> a row's budget within the 1.3 x guard on the load carried
    into it, following the rows as planned (plan_invariants.check_acwr)."""
    out, carried = {}, float(chronic)
    for w in weeks:
        if w.end < t:
            continue
        days = (w.end - w.start).days + 1
        load = sum(float(s.tss_estimate or 0) for s in w.sessions
                   if s.session_type != "rest" and s.status not in ("missed", "dismissed"))
        out[w.start] = min(w.tss_target, pi.ACWR_SWEET_SPOT * carried * days / 7)
        carried = pi.chronic_after(carried, load * 7 / days, days)
    return out


def short_weeks(weeks, goal, t, chronic=None):
    cap = guarded(weeks, t, chronic) if chronic else {}
    return [(str(w.start), w.tss_target, week_load(w, t)) for w in load_weeks(weeks, goal, t)
            if week_load(w, t) < 0.95 * cap.get(w.start, w.tss_target)
            and not out_of_room(w, goal, t)]


@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_9_1_the_owners_load_weeks_carry_their_budget(kind):
    """The owner's plan (12.5 h over six days): on d190fa82 its load weeks
    carried 0.80-0.85 of their budgets -- five or six file-length rides where
    the week had room and hours for more. Each load week now reaches its
    budget -- within the 1.3 x guard on the load planned before it (9.2) --
    or runs out of room (every easy ride at its date's minutes, or the week
    at its hours). The contract's aggregate figures (generate
    >= 0.90, the owner's regenerate >= 0.95) are the sweeps'
    (zz_probe/delivery.py): this owner's 2 h weekdays run out of room."""
    g = F.owner(kind)
    weeks = F.gen(g)
    lw = load_weeks(weeks, g, F.G0)
    assert len(lw) >= 3
    assert not short_weeks(weeks, g, F.G0, tp.athlete_weekly_load(55, 380))


@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_9_2_the_ramp_follows_the_filled_week(kind, monkeypatch):
    """The fill happens where LoadRamp.follow reads the week: the load the
    ramp carries forward is the load the week holds, not the sampler's
    file-length sketch of it."""
    seen = []
    follow = tp.LoadRamp.follow

    def spy(self, pw, budget):
        seen.append((pw, budget, tp._row_load(pw, budget), out_of_room(pw, g, F.G0)))
        return follow(self, pw, budget)
    monkeypatch.setattr(tp.LoadRamp, "follow", spy)
    g = F.owner(kind)
    F.gen(g)
    ok = {id(w) for w in load_weeks([x[0] for x in seen], g, F.G0)}
    followed = [(str(pw.start), b, round(r), full) for pw, b, r, full in seen if id(pw) in ok]
    assert len(followed) >= 3
    assert all(r >= 0.95 * b or full for _, b, r, full in followed), followed


@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_9_1_every_rebuilding_entry_point_fills(kind):
    """Regenerate, recalculate and extend build weeks too: theirs carry the
    budget as generate's do."""
    base = F.gen(F.owner(kind))
    for name, weeks in F.entry_points(kind, base, F.T3, lambda: F.owner(kind)):
        if name not in ("regenerate", "recalculate", "extend"):
            continue
        g = F.owner(kind)
        assert [w for w in load_weeks(weeks, g, F.T3) if w.start > F.T3], name
        assert not short_weeks(weeks, g, F.T3, 380), name


@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_9_2_the_guard_still_holds(kind):
    """The owner accepted chronic -- and so later budgets -- rising with the
    filled weeks, up to the 1.3 x guard: no week asks more than 1.3 x the
    load it carries (the auditor's ACWR rule), and none exceeds its date's
    minutes."""
    g = F.owner(kind)
    weeks = F.gen(g)
    assert not pi.check_acwr(weeks, tp.athlete_weekly_load(55, 380), today=F.G0)
    for w in weeks:
        for s in w.sessions:
            if s.session_type != "rest" and not s.is_race:
                assert s.duration_min <= tp._date_cap_min(g, s.day, {})[0], (s.day, s.duration_min)


def _fill_week():
    mon = _dt.date(2026, 9, 21)
    lib = tp.load_workout_library()
    z2 = next(r for r in lib if tp._content_class_for_row(r) == "endurance"
              and float(r["Duration(min)"]) == 60)
    vo2 = next(r for r in lib if tp._content_class_for_row(r) == "vo2max")

    def s(i, t, row, **kw):
        x = tp.PlannedSession(day=mon + _dt.timedelta(days=i), day_name="", session_type=t,
                              duration_min=int(float(row["Duration(min)"])),
                              tss_estimate=float(row["TSS"]), description="x",
                              zwo_file=row["File"])
        for k, v in kw.items():
            setattr(x, k, v)
        return x
    ss = [s(0, "vo2max", vo2), s(1, "z2", z2), s(2, "z2", z2, adapted=True),
          s(3, "z2", z2, user_moved=True), s(4, "recovery", z2), s(5, "long_z2", z2),
          s(6, "z2", z2, is_opener=True)]
    return tp.PlannedWeek(week_num=2, start=mon, end=mon + _dt.timedelta(days=6),
                          phase="build1", tss_target=600, is_stepback=False, sessions=ss), lib


def test_9_1_only_pending_easy_rides_grow_within_the_day_and_the_week():
    clock.freeze(_dt.date(2026, 9, 21))
    g = F.owner("continuous")
    w, lib = _fill_week()
    was = [(s.session_type, s.duration_min, s.tss_estimate, s.zwo_file) for s in w.sessions]
    tp._fill_easy_to_budget(w, g, lib)
    now = [(s.session_type, s.duration_min, s.tss_estimate, s.zwo_file) for s in w.sessions]
    grew = [i for i in range(7) if now[i] != was[i]]
    assert grew == [1, 5], grew                      # the plain z2 and the long ride
    for i in grew:
        s = w.sessions[i]
        assert s.duration_min <= tp._date_cap_min(g, s.day, {})[0]
        row = next(r for r in lib if r["File"] == s.zwo_file)
        assert abs(float(row["Duration(min)"]) - s.duration_min) <= tp._slot_file_band_min(s.duration_min)
        assert tp._content_class_for_row(row) == "endurance"
    assert tp._row_minutes(w, clock.today()) <= 12.5 * 60


def test_9_1_the_fill_stops_at_the_budget_and_skips_taper_weeks():
    clock.freeze(_dt.date(2026, 9, 21))
    g = F.owner("continuous")
    w, lib = _fill_week()
    load0 = week_load(w, clock.today())
    w.tss_target = load0 + 30
    tp._fill_easy_to_budget(w, g, lib)
    assert load0 + 20 <= week_load(w, clock.today()) <= load0 + 40
    # an unload week grows too, to its own budget (9.10), its long ride
    # within the stepback cap
    w, lib = _fill_week()
    w.is_stepback, w.tss_target = True, load0 + 60
    tp._fill_easy_to_budget(w, g, lib)
    assert load0 + 40 <= week_load(w, clock.today()) <= load0 + 70
    assert all(s.duration_min <= tp.STEPBACK_LONG_RIDE_CAP_MIN for s in w.sessions
               if s.session_type in ("z2", "long_z2"))
    # a taper week is shaped by its own passes
    w, lib = _fill_week()
    w.phase = "taper"
    tp._fill_easy_to_budget(w, g, lib)
    assert week_load(w, clock.today()) == load0


def test_9_4_a_lengthened_ride_serves_a_file_that_fits_it():
    """L-4's rule after the fill: a ride grown past its file's band serves an
    easy file of the new length, carrying at least what the shorter ride
    did, and its TSS is that file's load over the slot (6.8)."""
    clock.freeze(_dt.date(2026, 9, 21))
    g = F.owner("continuous")
    w, lib = _fill_week()
    rows = {r["File"]: r for r in lib}
    was = {s.day: (s.duration_min, s.tss_estimate, s.zwo_file) for s in w.sessions}
    tp._fill_easy_to_budget(w, g, lib)
    grown = [s for s in w.sessions if s.duration_min > was[s.day][0]]
    assert grown
    for s in grown:
        row = rows[s.zwo_file]
        fd = float(row["Duration(min)"])
        assert abs(fd - s.duration_min) <= tp._slot_file_band_min(s.duration_min), (s.day, fd, s.duration_min)
        assert s.zwo_file != was[s.day][2]           # the 60-min file does not fit 100+ min
        assert s.tss_estimate >= was[s.day][1]
        assert abs(s.tss_estimate - float(row["TSS"]) * s.duration_min / fd) <= 1


# ── 9.7 A reasoned rest does not reshape the next recovery week ──────────

def _rest(s, reason):
    s.session_type, s.duration_min, s.tss_estimate = "rest", 0, 0
    s.zwo_file, s.zwo_name, s.rest_reason, s.adapted = "", "", reason, True


def _unload_after_a_rested_build_day(reason):
    """The owner's plan; the first training day after today in the first full
    load week of the first block is rested with ``reason`` (the ride-sync
    adaptation writes "budget" after a heavy ride), and the final step runs
    again as every entry point does. Returns the block's unload week."""
    g = F.owner("continuous")
    weeks = F.gen(g)
    t = F.G0
    ui = next(i for i, w in enumerate(weeks) if tp._is_unload_week(w) and w.phase != "taper")
    bi = next(i for i in range(ui) if (weeks[i].end - weeks[i].start).days >= 6)
    if reason:
        day = next(s for s in weeks[bi].sessions if s.day > t and s.session_type != "rest")
        _rest(day, reason)
    clock.freeze(t)
    tp.finalize_weeks(weeks, g, normal_week=380)
    w = weeks[ui]
    return ([(s.day, s.session_type, s.duration_min, getattr(s, "rest_reason", "")) for s in w.sessions],
            week_load(w, t))


@pytest.mark.parametrize("reason", ["budget", "readiness"])
def test_9_7_a_reasoned_rest_in_a_build_week_leaves_the_unload_week_alone(reason):
    """The 2026-09-25 live case: Friday rested by the week's budget after a
    heavy ride, and at the next sync the unload week 10-05 lost a ride
    (10-06 rested "unload") and carried 204 of its 244. The build week's rest
    days are the rider's planned pattern, and a week the budget or readiness
    rested is not the block's lightest reference."""
    assert _unload_after_a_rested_build_day(reason) == _unload_after_a_rested_build_day(None)


def test_9_7_the_auditor_reads_the_lightest_week_the_same_way():
    """check_stepback_lightest: a build week holding a budget or readiness
    rest is not the lightest reference (unless every build week holds one);
    the control -- the same week without the reason -- is still one."""
    import plan_invariants as pi
    mon = _dt.date(2026, 9, 7)

    def wk(i, loads, stepback=False, rest_reason=""):
        start = mon + _dt.timedelta(weeks=i)
        ss = []
        for d, load in enumerate(loads):
            s = tp.PlannedSession(day=start + _dt.timedelta(days=d), day_name="",
                                  session_type="rest" if load == 0 else "z2",
                                  duration_min=0 if load == 0 else 60, tss_estimate=float(load),
                                  description="x")
            if load == 0 and d == 2:
                s.rest_reason = rest_reason
            ss.append(s)
        return tp.PlannedWeek(week_num=i + 1, start=start, end=start + _dt.timedelta(days=6),
                              phase="build1", tss_target=300, is_stepback=stepback, sessions=ss)
    full = [60, 60, 60, 60, 60, 0, 0]
    light = [60, 60, 0, 60, 60, 0, 0]            # Wednesday rested
    for reason, fires in (("budget", False), ("readiness", False), ("", True)):
        weeks = [wk(0, full), wk(1, light, rest_reason=reason), wk(2, full),
                 wk(3, [50, 50, 50, 50, 50, 0, 0], stepback=True)]   # 250 > 240
        got = [v.rule for v in pi.check_stepback_lightest(weeks)]
        assert ("stepback_lightest" in got) == fires, (reason, got)
    # ...unless every build week holds one: then they are the block
    weeks = [wk(0, light, rest_reason="budget"), wk(1, light, rest_reason="budget"),
             wk(2, [60, 60, 60, 60, 60, 0, 0], stepback=True)]
    assert "stepback_lightest" in [v.rule for v in pi.check_stepback_lightest(weeks)]


def test_9_7_an_unload_week_holding_only_the_test_closes_its_block():
    """The auditor kept a build week of the block before an unload week that
    held nothing but the FTP test as the next unload's lightest reference;
    the planner's block rule (_is_unload_week) had closed it. Control: the
    same plan with a light week in the second block still fires."""
    import plan_invariants as pi
    mon = _dt.date(2026, 9, 7)

    def wk(i, loads, stepback=False, test=False):
        start = mon + _dt.timedelta(weeks=i)
        ss = [tp.PlannedSession(day=start + _dt.timedelta(days=d), day_name="",
                                session_type="rest" if load == 0 else "z2",
                                duration_min=0 if load == 0 else 60, tss_estimate=float(load),
                                description="x") for d, load in enumerate(loads)]
        if test:
            ss[3].session_type, ss[3].tss_estimate, ss[3].duration_min = "ftp_test", 80.0, 52
        return tp.PlannedWeek(week_num=i + 1, start=start, end=start + _dt.timedelta(days=6),
                              phase="build1", tss_target=300, is_stepback=stepback, sessions=ss)
    full, light, zero = [60] * 5 + [0, 0], [40] * 5 + [0, 0], [0] * 7
    unload = [50] * 5 + [0, 0]                                   # 250
    weeks = [wk(0, light), wk(1, full), wk(2, zero, stepback=True, test=True),
             wk(3, full), wk(4, full), wk(5, unload, stepback=True)]
    assert "stepback_lightest" not in [v.rule for v in pi.check_stepback_lightest(weeks)]
    weeks[3] = wk(3, light)
    assert "stepback_lightest" in [v.rule for v in pi.check_stepback_lightest(weeks)]


def test_9_7_a_build_weeks_rest_days_are_the_riders_pattern():
    """Issue #4's step rests an unload week's shortest ride until it has more
    rest days than every build week of its block. Counting the day the budget
    took (rest_reason "budget") as one of them rested one ride more. The
    rider's pattern here is Friday; the unload week's two rest days already
    out-rest it."""
    mon = _dt.date(2026, 9, 7)
    g = F.owner("continuous")

    def wk(i, rest_days, stepback=False, budget_day=None, load=60):
        start = mon + _dt.timedelta(weeks=i)
        ss = []
        for d in range(7):
            s = tp.PlannedSession(day=start + _dt.timedelta(days=d), day_name="", session_type="z2",
                                  duration_min=90, tss_estimate=float(load), description="x",
                                  zwo_file=F.Z2)
            if d in rest_days:
                _rest(s, "")
            if d == budget_day:
                _rest(s, "budget")
            ss.append(s)
        return tp.PlannedWeek(week_num=i + 1, start=start, end=start + _dt.timedelta(days=6),
                              phase="build1", tss_target=float(6 * load),
                              is_stepback=stepback, sessions=ss)
    clock.freeze(mon)
    weeks = [wk(0, [4]), wk(1, [4], budget_day=2), wk(2, [4]),
             wk(3, [4, 6], stepback=True, load=40)]           # 200 TSS, under 0.9 x 300
    weeks[3].tss_target = 180.0      # its floor (0.6 / 0.72 of it) leaves a ride to rest
    tp._enforce_stepback_is_lightest(weeks, g, {})
    assert [s.session_type for s in weeks[3].sessions].count("rest") == 2


# ── 9.6 The rider sees the truth ─────────────────────────────────────────

def _stored_week(minutes, tss, budget, hpw):
    mon = _dt.date(2026, 9, 21)
    ss = [{"day": (mon + _dt.timedelta(days=i)).isoformat(), "session_type": "z2",
           "duration_min": minutes, "tss_estimate": tss, "zwo_file": "", "status": "pending"}
          for i in range(6)] + [{"day": (mon + _dt.timedelta(days=6)).isoformat(),
                                 "session_type": "rest", "duration_min": 0, "tss_estimate": 0}]
    return mon, {"goal": {"type": "continuous", "hours_per_week": hpw},
                 "weeks": [{"week_num": 2, "start": mon.isoformat(),
                            "end": (mon + _dt.timedelta(days=6)).isoformat(), "phase": "build1",
                            "tss_target": budget, "is_stepback": False, "sessions": ss}]}


@pytest.mark.parametrize("minutes, hpw, reason, stepback", [
    (80, 8.0, "hours", False), (60, 8.0, "other", False), (60, 8.0, "unload", True)])
def test_9_6_the_week_view_carries_budget_planned_and_why(minutes, hpw, reason, stepback):
    """A week planned under its budget by more than the tolerance says so,
    with both numbers and the reason: the rider's weekly hours are full, a
    recovery week kept light (never filled), or something else held it (the
    week's days, the guard, the structure)."""
    import week_view
    mon, plan = _stored_week(minutes, 50, 400.0, hpw)
    plan["weeks"][0]["is_stepback"] = stepback
    got = week_view.build(plan, mon, {}, lambda r: "z2").as_weekly_plan()
    assert got["tss_budget"] == 400.0 and got["planned_tss"] == 300.0
    assert got["short_of_budget"] == {"budget": 400.0, "planned": 300.0, "reason": reason}


def test_9_6_a_week_within_the_tolerance_is_not_short():
    import week_view
    mon, plan = _stored_week(80, 50, 312.0, 8.0)                  # 300 of 312: 4 % under
    assert week_view.build(plan, mon, {}, lambda r: "z2").as_weekly_plan()["short_of_budget"] is None
    assert week_view.BUDGET_TOLERANCE == tp._VOLUME_CEILING_TOLERANCE


def test_9_6_the_dashboard_names_the_planned_load_and_the_budget():
    """The This Week badge read "TSS target: <planned sum>". It names the
    planned load as planned, and a week short of its budget shows the budget
    and the reason."""
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    from test_333_owner_fixes import DASHBOARD, _extract_js_function
    fn = _extract_js_function(DASHBOARD.read_text(encoding="utf-8"), "_weekLoadText")
    harness = fn + """
const a = _weekLoadText({planned_tss: 300.4, tss_budget: 400, short_of_budget:
                         {budget: 400, planned: 300.4, reason: 'hours'}});
if (!/planned 300 TSS/.test(a) || !/400/.test(a) || !/hours/.test(a) || /target/i.test(a))
  throw new Error('short week: ' + a);
const b = _weekLoadText({planned_tss: 300, tss_budget: 400, short_of_budget:
                         {budget: 400, planned: 300, reason: 'other'}});
if (!/planned 300 TSS/.test(b) || !/400/.test(b) || /hours/.test(b)) throw new Error('other: ' + b);
const r = _weekLoadText({planned_tss: 434, tss_budget: 479, short_of_budget:
                         {budget: 479, planned: 434, reason: 'ridden', ridden: 503}});
if (!/503 TSS already ridden/.test(r) || /plan holds less/.test(r)) throw new Error('ridden: ' + r);
const u = _weekLoadText({planned_tss: 261, tss_budget: 387, short_of_budget:
                         {budget: 387, planned: 261, reason: 'unload'}});
if (!/recovery week/.test(u) || /could not/.test(u)) throw new Error('unload: ' + u);
if (_weekLoadText({planned_tss: 5, short_of_budget: {budget: null, planned: 5, reason: 'other'}})
    !== 'planned 5 TSS') throw new Error('no budget');
const c = _weekLoadText({planned_tss: 305, tss_budget: 310, short_of_budget: null});
if (c !== 'planned 305 TSS') throw new Error('held week: ' + c);
if (_weekLoadText({planned_tss: null}) !== 'no plan on record') throw new Error('no plan');
console.log('OK');
"""
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0 and "OK" in res.stdout, res.stderr + res.stdout


def test_9_1_the_week_under_way_is_filled_against_what_is_left():
    """The final step hands the fill the week under way's net budget (what
    is left of it, D2); the load held against it is the days from today, not
    the days behind the rider too (the review of 254d54eb: a target of 349
    against a load of 414 of which 269 lay ahead, and no growth)."""
    g = F.owner("continuous")
    w, lib = _fill_week()
    clock.freeze(_dt.date(2026, 9, 23))                  # Wednesday: Mon, Tue behind
    ahead = week_load(w, clock.today())
    tp._fill_easy_to_budget(w, g, lib, target=ahead + 40)
    assert ahead + 30 <= week_load(w, clock.today()) <= ahead + 50


# ── M1 (evaluation of a73647b3): a ride done today is ridden ─────────────

def _thursday_regenerate(kind, today_tss, monkeypatch):
    """The owner's plan regenerated on Thursday 09-24 of its second week:
    Monday ridden, Tuesday missed, Wednesday ridden, and a ride done today
    before the regenerate (the owner's 2026-09-24: 280 TSS in the evening).
    Returns (the ramp's budget for the rebuilt stub row from today, its
    span, the load planned after today, ridden today, ridden since Monday)."""
    t = _dt.date(2026, 9, 24)
    base = F.gen(F.owner(kind))
    for w in base:
        for s in w.sessions:
            if s.day == t - _dt.timedelta(days=2) and s.session_type != "rest":
                s.status = "missed"
    # the week before ridden as planned (no absence, so no recovery ramp)
    rides = F.ride_days(t - _dt.timedelta(days=10), t - _dt.timedelta(days=4))
    rides += [{"date": (t - _dt.timedelta(days=3)).isoformat(), "tss": 100.0},
              {"date": (t - _dt.timedelta(days=1)).isoformat(), "tss": 40.0}]
    if today_tss:
        rides.append({"date": t.isoformat(), "tss": float(today_tss)})
    stub = []
    follow = tp.LoadRamp.follow

    def spy(self, pw, budget):
        if pw.start == t:
            stub.append((budget, (pw.end - pw.start).days + 1))
        return follow(self, pw, budget)
    monkeypatch.setattr(tp.LoadRamp, "follow", spy)
    filled, fill = [], tp._fill_easy_to_budget

    def fill_spy(pw, *a, **k):
        if pw.start == t:                        # the builder stub row
            left = (tp._row_net(pw, t, k.get("rides")) if hasattr(tp, "_row_net")
                    else pw.tss_target)          # d190fa82..a73647b3: no net here
            filled.append(left)
        return fill(pw, *a, **k)
    monkeypatch.setattr(tp, "_fill_easy_to_budget", fill_spy)
    clock.freeze(t)
    g = F.owner(kind)
    g.plan_weeks = 0
    weeks = tp.regenerate_from_today(g, copy.deepcopy(base), 55, activities=rides,
                                     athlete=F.ATH, recent_weekly_tss=380)[1]
    monkeypatch.undo()
    wk = next(w for w in weeks if w.start <= t <= w.end)
    after = sum(float(s.tss_estimate or 0) for s in wk.sessions
                if s.day > t and s.session_type != "rest" and not s.is_race)
    budget, span = stub[0]
    since_monday = sum(r["tss"] for r in rides if r["date"] >= (t - _dt.timedelta(days=3)).isoformat())
    return budget, span, after, float(today_tss), since_monday, filled, wk


@pytest.mark.parametrize("kind", ["continuous", "event"])
def test_m1_a_regenerate_counts_the_ride_done_today(kind, monkeypatch):
    """D2 for the week under way: after today it may carry the rebuilt row's
    budget less what was ridden in it, and the week's gross less what was
    ridden since Monday -- today's ride included; a missed day's ask is not
    budget to hand out again (D6). On a73647b3 the ride today was invisible
    to the builder's stub row and to the final step, and the merged row's
    target carried the missed Tuesday: the owner's Friday grew 55 -> 180 min."""
    budget, span, after, today, since_monday, filled, wk = _thursday_regenerate(kind, 100, monkeypatch)
    # ``budget`` is the ramp's week; the stub row carries its days' share
    left = max(0.0, min(budget * span / 7 - today, budget - since_monday))
    # one short spin may stay when the rides alone spent the week (6.7)
    assert after <= max(left * 1.05 + 1, 60), (budget, span, since_monday, after)
    # ...and the builder's fill (9.2: the load the ramp follows) holds the
    # stub row to what is left once today's ride is counted
    assert filled and all(x is not None and x <= left + 1 for x in filled), (left, filled)
    # ...and the row, widened back to Monday by the record step, carries the
    # ramp's budget for the week -- not the old plan's asks, missed ones
    # included, on top of the stub's (the owner's week read 670 for 532)
    assert (wk.end - wk.start).days == 6 and abs(wk.tss_target - budget) <= 1, (wk.tss_target, budget)


# ── S4 (evaluation of a73647b3): the fill's own hours limit ─────────────

def test_9_3_the_fill_itself_stays_within_the_weekly_hours():
    """The final step's hours trim would mask a fill that ignored the week's
    hours (the evaluator's mutant fill_ignores_hours passed every test), so
    the fill is held to them on its own: a 600-TSS budget, days that could
    hold far more, and 7 h a week."""
    clock.freeze(_dt.date(2026, 9, 21))
    g = F.owner("continuous", hours_per_week=7.0)
    w, lib = _fill_week()
    was = tp._row_minutes(w, clock.today())
    assert was < 7 * 60
    tp._fill_easy_to_budget(w, g, lib)
    grown = tp._row_minutes(w, clock.today())
    assert was < grown <= 7 * 60 + 1, (was, grown)


# ── 9.9 The 1.3 x guard runs on every sync ───────────────────────────────

def stored(weeks, goal):
    """The plan as the app keeps it on disk, through the planner's codec
    (not tests/_gate_env: importing it pins the process's clock for good)."""
    return {"goal": tp.goal_to_dict(goal), "weeks": [tp.week_to_dict(w) for w in weeks]}


def read_stored(plan):
    return [tp.week_from_dict(w) for w in plan["weeks"]]


def _weekend_away(gate_seed_unused=None):
    t0, t = _dt.date(2026, 9, 14), _dt.date(2026, 9, 17)
    g = lambda: sweep_goal("event", t0, rest=[0, 1, 2, 3, 4], daily={5: 4.0, 6: 4.0},
                           hpw=8, race_in=8)
    base = sweep_gen(g(), t0, 40, 250)
    clock.freeze(t)
    away = {"2026-09-19": 0.0, "2026-09-20": 0.0}
    return g, base, t, away


# The weekend rider carried 250 on Monday 09-14 and rides no weekday: on
# Thursday the archive reads that load three days decayed. The auditor
# follows the plan from the Monday (the evaluator's "follow" measure), so
# the week under way is counted once.
T0 = _dt.date(2026, 9, 14)
LOAD_AT_T = round(pi.chronic_after(250, 0, 3), 1)


def test_9_9_a_sync_brings_the_weeks_after_a_lighter_stretch_down(gate_seed):
    """weekend-event8 (weekends only, carrying 250 a week), planned on 09-14;
    at the 09-17 sync the rider marks that weekend away. The week after asked
    2 h tempo + 4 h z2 = 310 TSS against the 152 the rider would carry into
    it (2.04 x; d190fa82 1.13 x). The sync caps every week from today on at
    1.3 x the load carried into it -- the rider's real load, following the
    weeks as planned -- and never raises one."""
    g, base, t, away = _weekend_away()
    before = {w.start: w.tss_target for w in base}
    weeks = tp.reforecast(g(), copy.deepcopy(base), recent_activities=[],
                          availability_overrides=away, recent_weekly_tss=LOAD_AT_T)[0]
    assert not pi.check_acwr(weeks, 250, today=T0)
    assert all(w.tss_target <= before[w.start] for w in weeks if w.start in before)


def test_9_9_every_sync_holds_the_guard(gate_seed):
    """reforecast_dict (what every sync runs) and refit hold it too."""
    g, base, t, away = _weekend_away()
    plan = stored(copy.deepcopy(base), g())
    tp.reforecast_dict(plan, recent_activities=[], availability_overrides=away,
                       recent_weekly_tss=LOAD_AT_T)
    assert not pi.check_acwr(read_stored(plan), 250, today=T0)


@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_9_9_every_entry_point_holds_the_guard(kind):
    """The owner after every entry point, each handed the rider's load
    (380) as production reads it from the ride archive. The weeks after the
    one under way: the auditor reads that one's days behind today off the
    plan, and the harness's rides (60 TSS a day) are not what it planned."""
    base = F.gen(F.owner(kind))
    for name, weeks in F.entry_points(kind, base, F.T3, lambda: F.owner(kind)):
        later = {w.week_num for w in weeks if w.start > F.T3}
        assert not [v for v in pi.check_acwr(weeks, 380, today=F.T3)
                    if v.week_num in later], name


def test_9_9_a_sync_reads_the_riders_load_from_the_ride_archive(gate_seed, monkeypatch):
    """The app hands a sync no load: the final step reads the archive
    (ride_storage.chronic_weekly_tss, each ride once, cycling only)."""
    import ride_storage
    g, base, t, away = _weekend_away()
    monkeypatch.setattr(ride_storage, "chronic_weekly_tss", lambda *a, **k: LOAD_AT_T)
    plan = stored(copy.deepcopy(base), g())
    tp.reforecast_dict(plan, recent_activities=[], availability_overrides=away)
    assert not pi.check_acwr(read_stored(plan), 250, today=T0)


# ── 9.10 Unload weeks carry their own budget ─────────────────────────────

@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_9_10_an_unload_week_carries_its_own_budget(kind):
    """The owner's regenerate on 2026-09-24 left 10-05 at 261 of its 387
    budget -- under the unload's own floor (STEPBACK_DEEPEST / 0.72 of it,
    322) and a 52 % cut from the 540 before it, where the design is 0.72.
    The fill grows an unload week toward its budget, easy volume only and
    its long ride within the stepback cap; the unload rules still hold:
    lighter than the lightest full load week of its block, never over its
    budget."""
    g = F.owner(kind)
    weeks = F.gen(g)
    floor = tp.STEPBACK_DEEPEST / tp.STEPBACK_LOAD_FACTOR
    # training load, as the unload pass and the auditor count it: the FTP
    # test is a measurement placed on fresh legs, not a dose
    train = lambda w: sum(float(s.tss_estimate or 0) for s in pi._training(w))
    seen = 0
    block = []
    for w in weeks:
        full = (w.end - w.start).days == 6
        if not w.is_stepback:
            if full and not tp._is_unload_week(w) and w.phase != "taper":
                block.append(train(w))
            continue
        if not full or w.phase == "taper" or tp._race_in_week(w, g) or not block:
            block = []
            continue
        seen += 1
        load = train(w)
        assert floor * w.tss_target <= week_load(w, F.G0) <= w.tss_target * 1.05 + 1, \
            (str(w.start), w.tss_target, week_load(w, F.G0))
        assert load <= tp.STEPBACK_BELOW_LIGHTEST * min(block) + 1, (str(w.start), load, block)
        for s in w.sessions:
            if s.session_type in ("z2", "long_z2"):
                assert s.duration_min <= tp.STEPBACK_LONG_RIDE_CAP_MIN
        block = []
    assert seen >= 1


def test_9_6_a_week_the_rides_spent_says_so():
    """The owner's week of 09-21 after the 09-25 sync: 434 planned of a 479
    budget, 503 ridden, Friday rested by the budget. It read "the plan holds
    less this week" (the evaluation of a73647b3, S3); the reason is the
    rides, and the view carries what they came to."""
    import week_view
    mon = _dt.date(2026, 9, 21)
    clock.freeze(_dt.date(2026, 9, 25))
    load = [(135, "done"), (135, "missed"), (96, "done_partial"), (68, "pending")]
    ss = [{"day": (mon + _dt.timedelta(days=i)).isoformat(), "session_type": "z2",
           "duration_min": 90, "tss_estimate": t, "zwo_file": "", "status": st}
          for i, (t, st) in enumerate(load)]
    ss += [{"day": (mon + _dt.timedelta(days=4)).isoformat(), "session_type": "rest",
            "duration_min": 0, "tss_estimate": 0, "rest_reason": "budget"}]
    plan = {"goal": {"type": "continuous", "hours_per_week": 15.0},
            "weeks": [{"week_num": 1, "start": mon.isoformat(),
                       "end": (mon + _dt.timedelta(days=6)).isoformat(), "phase": "continuous",
                       "tss_target": 479.0, "is_stepback": False, "sessions": ss}]}
    rides = [{"date": "2026-09-21", "tss": 205.0}, {"date": "2026-09-23", "tss": 18.0},
             {"date": "2026-09-24", "tss": 280.0}]
    got = week_view.build(plan, mon, {}, lambda r: "z2", rides=rides).as_weekly_plan()
    assert got["short_of_budget"] == {"budget": 479.0, "planned": 434.0, "reason": "ridden",
                                      "ridden": 503.0}
    # without the rides, as before: no reason to name them
    got = week_view.build(plan, mon, {}, lambda r: "z2").as_weekly_plan()
    assert got["short_of_budget"]["reason"] == "other"


# ── MF-A (evaluation of f59ccdee): a gate that pollutes is not a gate ────

def test_no_test_module_imports_the_gate_env_in_process():
    """tests/_gate_env.py freezes the planner's clock at 2026-09-14 for good,
    repoints DOMESTIQUE_HOME and pins the athlete id, for the rest of the
    process: imported by two tests here it failed test_v175_apply_tier_down
    serially ("No session at 2026-09-14") and whatever shared an xdist
    worker. Only the characterization and parity scripts, which run in their
    own process, may import it."""
    import ast
    bad = []
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            if "_gate_env" in names:
                bad.append(f"{f.name}:{node.lineno}")
    assert not bad, bad


def test_the_gate_env_removes_its_data_home():
    """Each import made a /tmp/domestique-gate-* directory and left it."""
    import os
    import subprocess
    out = subprocess.run(
        [sys.executable, "-c", "import os, sys; sys.path.insert(0, 'tests'); import _gate_env; "
                               "print(os.environ['DOMESTIQUE_HOME'])"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
        env={**os.environ, "DOMESTIQUE_NO_NET": "1"})
    home = out.stdout.strip().splitlines()[-1]
    assert home.startswith("/") and "domestique-gate-" in home, out.stdout + out.stderr
    left = os.path.exists(home)
    if left:
        import shutil
        shutil.rmtree(home, ignore_errors=True)
    assert not left, home


# ── MF-B (evaluation of f59ccdee): the week under way is in the block ────

def _block_underway():
    """A build week under way (Mon 09-28 .. Sun, today Friday 10-02) and the
    unload week after it: the build is the block's only full week."""
    mon = _dt.date(2026, 9, 28)

    def wk(i, loads, stepback=False, target=400.0):
        start = mon + _dt.timedelta(weeks=i)
        ss = [tp.PlannedSession(day=start + _dt.timedelta(days=d), day_name="",
                                session_type="rest" if load == 0 else "z2",
                                duration_min=0 if load == 0 else 90, tss_estimate=float(load),
                                description="x", zwo_file=F.Z2)
              for d, load in enumerate(loads)]
        return tp.PlannedWeek(week_num=i + 1, start=start, end=start + _dt.timedelta(days=6),
                              phase="continuous", tss_target=target, is_stepback=stepback,
                              sessions=ss)
    return [wk(0, [60, 60, 60, 60, 66, 0, 0], target=306.0),
            wk(1, [72, 72, 72, 72, 71, 0, 0], stepback=True, target=359.0)]


def test_mf_b_an_unload_week_is_lighter_than_the_build_under_way():
    """owner-cont, planned 09-14, synced 10-02: the only build of the block
    was under way; bbbb15fe left it out, the unload pass had no reference,
    and the unload week went to 359 against the build's 306 (d190fa82 221
    against 256). The planner holds it under 0.9 x the build at its planned
    load, and the auditor reads the block the same way."""
    import plan_invariants as pi_
    clock.freeze(_dt.date(2026, 10, 2))
    weeks = _block_underway()
    assert "stepback_lightest" in [v.rule for v in pi_.check_stepback_lightest(weeks, _dt.date(2026, 10, 2))]
    tp._enforce_stepback_is_lightest(weeks, F.owner("continuous"), {})
    unload = sum(s.tss_estimate for s in weeks[1].sessions if s.session_type != "rest")
    assert unload <= 0.9 * 306 + 1, unload
    assert not pi_.check_stepback_lightest(weeks, _dt.date(2026, 10, 2))


# ── 9.9 amended: the guard tracks, it does not ratchet ───────────────────

def _owner_weeks():
    g = F.owner("continuous")
    return g, F.gen(g)


def test_9_9_a_lowered_week_climbs_back_to_its_built_budget():
    """A light stretch lowers the weeks ahead and records it (9.11); when the
    load returns the next sync restores them, never above the budget they
    were built with, and the record goes."""
    g, weeks = _owner_weeks()
    t = _dt.date(2026, 9, 21)
    clock.freeze(t)
    built = {w.start: w.tss_target for w in weeks}
    tp.finalize_weeks(weeks, g, normal_week=200)
    low = [w for w in weeks if w.start >= t and w.tss_target < built[w.start]]
    assert low and all(w.guard_lowered and w.guard_lowered["from"] == built[w.start]
                       and w.guard_lowered["to"] == w.tss_target
                       and w.guard_lowered["when"] == "2026-09-21"
                       and w.guard_lowered["carried"] >= 200 for w in low)
    tp.finalize_weeks(weeks, g, normal_week=900)
    assert all(w.tss_target == built[w.start] and not w.guard_lowered
               for w in weeks if w.start >= t), [(w.start, w.tss_target, built[w.start]) for w in weeks]


def test_9_9_the_week_under_way_is_counted_once(monkeypatch):
    """A rider who rides the plan exactly, synced on Monday and again on
    Thursday: the budgets are the same both times. The guard read the
    Thursday load -- which already holds Mon-Wed -- as the load carried into
    the week under way, and then counted Mon-Wed again in the week's own
    load: a compliant rider was held 6-11 % low for the whole horizon (the
    evaluation of f59ccdee). The load carried into a week under way is the
    archive's reading on its eve."""
    import ride_storage
    g, weeks = _owner_weeks()
    mon = _dt.date(2026, 9, 28)
    thu = mon + _dt.timedelta(days=3)
    wk = next(w for w in weeks if w.start == mon)
    per_day = {s.day: float(s.tss_estimate or 0) for s in wk.sessions if s.session_type != "rest"}
    load_thu = 300.0
    for i in range(3):
        load_thu = pi.chronic_after(load_thu, 7 * per_day.get(mon + _dt.timedelta(days=i), 0.0), 1)
    # the archive: 300 on the eve of 09-28, then the plan ridden exactly
    monkeypatch.setattr(ride_storage, "chronic_weekly_tss",
                        lambda rides=None, today=None: 300.0 if today <= mon else load_thu)
    a, b = copy.deepcopy(weeks), copy.deepcopy(weeks)
    clock.freeze(mon)
    tp.finalize_weeks(a, g)
    rides = [{"date": d.isoformat(), "tss": v} for d, v in per_day.items() if d < thu]
    for w in b:
        for s in w.sessions:
            if s.day < thu and s.session_type != "rest":
                s.status = "done"
    clock.freeze(thu)
    tp.finalize_weeks(b, g, rides=rides)
    assert [(w.start, w.tss_target) for w in a if w.start >= mon] == \
           [(w.start, w.tss_target) for w in b if w.start >= mon]


# ── 9.11 A guard-lowered budget is visible, with the way back ────────────

def test_9_11_the_stored_week_carries_the_lowering_and_the_api_serves_it():
    """The guard's lowering is on the stored week (through the codec that
    writes the plan), and /api/weekly-plan's view serves that record -- served
    = stored -- until a sync restores the week."""
    import week_view
    g, weeks = _owner_weeks()
    t = _dt.date(2026, 9, 21)
    clock.freeze(t)
    tp.finalize_weeks(weeks, g, normal_week=200)
    plan = stored(weeks, g)
    low = [w for w in plan["weeks"] if w.get("guard_lowered")]
    assert low
    w = low[0]
    start = _dt.date.fromisoformat(w["start"])
    view = week_view.build(plan, start, {}, lambda r: "z2").as_weekly_plan()
    assert view["budget_lowered"] == w["guard_lowered"]
    assert read_stored(plan)[plan["weeks"].index(w)].guard_lowered == w["guard_lowered"]
    tp.finalize_weeks(weeks, g, normal_week=900)
    plan = stored(weeks, g)
    assert week_view.build(plan, start, {}, lambda r: "z2").as_weekly_plan()["budget_lowered"] is None


def test_9_11_the_dashboard_warns_for_every_lowered_week_ahead():
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    from test_333_owner_fixes import DASHBOARD, _extract_js_function
    src = DASHBOARD.read_text(encoding="utf-8")
    fn = _extract_js_function(src, "_guardLoweredHtml")
    harness = "const esc = s => String(s);\n" + fn + """
const weeks = [
  {start: '2026-09-21', end: '2026-09-27', tss_target: 300,
   guard_lowered: {from: 480, to: 300, when: '2026-09-22', carried: 231}},   // under way
  {start: '2026-09-28', end: '2026-10-04', tss_target: 350,
   guard_lowered: {from: 520, to: 350, when: '2026-09-22', carried: 231}},
  {start: '2026-10-05', end: '2026-10-11', tss_target: 400, guard_lowered: null},
  {start: '2026-09-14', end: '2026-09-20', tss_target: 200,
   guard_lowered: {from: 300, to: 200, when: '2026-09-15', carried: 150}},    // past
];
const h = _guardLoweredHtml(weeks, '2026-09-23');
for (const want of ['480', '300', '520', '350', '231', 'regeneratePlan()'])
  if (h.indexOf(want) < 0) throw new Error('missing ' + want + ': ' + h);
if (h.indexOf('150') >= 0) throw new Error('a past week warned: ' + h);
if ((h.match(/lowered from/g) || []).length !== 2) throw new Error('two weeks: ' + h);
if (_guardLoweredHtml([weeks[2]], '2026-09-23') !== '') throw new Error('no warning without a record');
console.log('OK');
"""
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0 and "OK" in res.stdout, res.stderr + res.stdout


# ── Minor (evaluation of f59ccdee) ───────────────────────────────────────

def test_a_blank_husk_dated_today_is_not_a_ride():
    """Strava's blank husk (no sport, no TSS) dated today read as "rode
    today": today's pending session left the load ahead and the week's
    remainder was measured from tomorrow."""
    t = _dt.date(2026, 9, 24)
    assert not tp._rode_on([{"date": t.isoformat(), "sport": "", "tss": None}], t)
    assert not tp._rode_on([{"date": t.isoformat()}], t)
    assert tp._rode_on([{"date": t.isoformat(), "sport": "Ride", "tss": 0}], t)
    assert tp._rode_on([{"date": t.isoformat(), "tss": 80.0}], t)
    assert not tp._rode_on([{"date": t.isoformat(), "sport": "Run", "tss": 80.0}], t)


def test_the_badge_reads_the_weeks_own_rides(tmp_path, monkeypatch):
    """/api/weekly-plan read the rides from /api/activities, whose fallback
    holds 7 days: last week's badge read "other" though its rides spent the
    budget. It reads the ride archive, which holds the week."""
    import json as _json
    import app as app_module
    import cache
    mon = _dt.date(2026, 9, 21)
    clock.freeze(_dt.date(2026, 9, 30))
    ss = [{"day": (mon + _dt.timedelta(days=i)).isoformat(), "session_type": "z2",
           "duration_min": 90, "tss_estimate": 100.0, "zwo_file": "", "status": "done"}
          for i in range(4)] + [{"day": (mon + _dt.timedelta(days=4)).isoformat(),
                                 "session_type": "rest", "duration_min": 0, "tss_estimate": 0,
                                 "rest_reason": "budget"}]
    plan = {"goal": {"type": "continuous", "hours_per_week": 15.0},
            "weeks": [{"week_num": 1, "start": mon.isoformat(),
                       "end": (mon + _dt.timedelta(days=6)).isoformat(), "phase": "continuous",
                       "tss_target": 479.0, "is_stepback": False, "sessions": ss}]}
    (tmp_path / "current_plan.json").write_text(_json.dumps(plan))
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(app_module, "get_today_metrics", lambda: {})
    monkeypatch.setattr(app_module, "fetch_wellness", lambda *a, **k: [])
    archive = [{"started_at": (mon + _dt.timedelta(days=i)).isoformat() + "T10:00:00",
                "sport": "Ride", "tss": 125.0, "ride_id": f"r{i}"} for i in range(4)]
    monkeypatch.setattr(app_module, "_load_all_rides_safe", lambda *a, **k: archive)
    monkeypatch.setattr(app_module, "api_activities", lambda: [])   # the 7-day list
    cache.clear_cache()
    got = app_module.api_weekly_plan(week_offset=-1)
    assert got["short_of_budget"] and got["short_of_budget"]["reason"] == "ridden", got["short_of_budget"]
    assert got["short_of_budget"]["ridden"] == 500.0


def test_9_9_the_week_under_way_is_read_from_its_rides(monkeypatch):
    """The owner's 09-25 sync: the week under way planned 299 on its days
    behind (a missed Tuesday) where 503 was ridden. Read off the plan, the
    load carried into 09-28 was 340 and the guard lowered that week from 469
    to 442; read from the rides, it stands."""
    import ride_storage
    g, weeks = _owner_weeks()
    mon, t = _dt.date(2026, 9, 21), _dt.date(2026, 9, 24)
    for w in weeks:
        for s in w.sessions:
            if mon <= s.day < t and s.session_type != "rest":
                s.status = "missed" if s.day == mon + _dt.timedelta(days=1) else "done"
    rides = [{"date": "2026-09-21", "tss": 205.0}, {"date": "2026-09-23", "tss": 18.0},
             {"date": "2026-09-24", "tss": 280.0}]
    monkeypatch.setattr(ride_storage, "chronic_weekly_tss", lambda rides=None, today=None: 367.0)
    nxt = mon + _dt.timedelta(days=7)
    built = next(w.tss_target for w in weeks if w.start == nxt)
    clock.freeze(t)
    tp.finalize_weeks(weeks, g, rides=rides)
    w = next(w for w in weeks if w.start == nxt)
    assert w.tss_target == built and not w.guard_lowered, (built, w.tss_target, w.guard_lowered)


# ── MF-1 (evaluation of 3793952c): one reader of the rider's load ────────

def _owner_archive():
    """The owner's week of 09-21 as the ride archive holds it (ICU records):
    Mon 205, Tue missed, Wed 18, Thu 280 -- and the weeks before at 380."""
    out = []
    d = _dt.date(2026, 8, 17)
    while d < _dt.date(2026, 9, 21):
        if d.weekday() != 4:
            out.append({"ride_id": f"icu_a{d.toordinal()}", "source": "icu", "sport": "Ride",
                        "started_at": d.isoformat() + "T10:00:00", "tss": 380 / 6})
        d += _dt.timedelta(days=1)
    for day, tss in (("2026-09-21", 205.0), ("2026-09-23", 18.0), ("2026-09-24", 280.0)):
        out.append({"ride_id": f"icu_{day}", "source": "icu", "sport": "Ride",
                    "started_at": day + "T10:00:00", "tss": tss})
    return out


def test_mf1_the_weekly_recalc_reads_the_rides_not_the_plan(tmp_path, monkeypatch):
    """POST /api/plan/auto-recalc on the owner's shape, 09-26: the weekly
    recalc (continuous -> extend) handed the final step no rides, so the
    guard read the week under way off the plan (a missed Tuesday: 299 where
    503 was ridden) and lowered 09-28 from its budget with a false "recent
    load 340". The planner reads the ride archive itself, whatever the
    caller passes."""
    import json as _json
    import app as A
    import ride_storage
    from fastapi.testclient import TestClient
    g, weeks = _owner_weeks()
    for w in weeks:
        for s in w.sessions:
            if _dt.date(2026, 9, 21) <= s.day < _dt.date(2026, 9, 25) and s.session_type != "rest":
                s.status = "missed" if s.day == _dt.date(2026, 9, 22) else "done"
    built = {w.start.isoformat(): w.tss_target for w in weeks}
    plan = stored(weeks, g)
    plan.update(generated="2026-09-14T08:00:00", recalc_date="2026-09-14T08:00:00")
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    (tmp_path / "current_plan.json").write_text(_json.dumps(plan))
    monkeypatch.setattr(ride_storage, "load_all_rides", _owner_archive)
    monkeypatch.setattr(A, "get_today_metrics", lambda: {})
    monkeypatch.setattr(A, "_planning_ctl", lambda *a, **k: 55.0)
    monkeypatch.setattr(A, "fetch_wellness", lambda *a, **k: [])
    clock.freeze(_dt.date(2026, 9, 26))
    A.clear_cache()
    r = TestClient(A.app, base_url="http://127.0.0.1").post("/api/plan/auto-recalc")
    assert r.status_code == 200, r.text[:300]
    got = {w["start"]: w for w in _json.loads((tmp_path / "current_plan.json").read_text())["weeks"]}
    w = got["2026-09-28"]
    assert w["tss_target"] == built["2026-09-28"] and not w.get("guard_lowered"), \
        (r.json().get("action"), built["2026-09-28"], w["tss_target"], w.get("guard_lowered"))


def test_mf3_a_monday_sync_reads_the_same_load_as_tuesdays(monkeypatch):
    """On a Monday the week ahead starts today and the guard read today's
    archive: its 7-day window counts the unridden Monday as a zero (Sunday
    343.5, Monday 299.4 on the owner's light run), and 10-12 dropped 396 ->
    361 every Monday, back on Tuesday. The load carried in is always the
    eve of the first week ahead."""
    import ride_storage
    g, weeks = _owner_weeks()
    mon = _dt.date(2026, 9, 28)
    readings = {mon - _dt.timedelta(days=1): 300.0, mon: 260.0}
    monkeypatch.setattr(ride_storage, "chronic_weekly_tss",
                        lambda rides=None, today=None: readings.get(today, 300.0))
    monkeypatch.setattr(ride_storage, "load_all_rides", lambda: [
        {"ride_id": "icu_x", "sport": "Ride", "started_at": "2026-09-27T10:00:00", "tss": 60.0}])
    a, b = copy.deepcopy(weeks), copy.deepcopy(weeks)
    clock.freeze(mon)
    tp.finalize_weeks(a, g)
    clock.freeze(mon + _dt.timedelta(days=1))
    tp.finalize_weeks(b, g)
    assert [(w.start, w.tss_target) for w in a if w.start > mon] == \
           [(w.start, w.tss_target) for w in b if w.start > mon]


def test_mf2_a_restored_week_gets_its_easy_volume_back():
    """A sync that lowered a week trims its sessions; when the load returns
    and a sync restores the budget, the easy volume grows back toward it
    (within the date and week caps, easy only). A sync never topped a week
    up, so the owner's 09-28 went 485 -> 423 planned with its budget back at
    469, and a light run's 10-12 went 351 -> 322 -> 295 planned while its
    budget went 396 -> 361 -> 396 (the evaluation of 3793952c, MF-2)."""
    g, weeks = _owner_weeks()
    t = _dt.date(2026, 9, 21)
    clock.freeze(t)
    nxt = _dt.date(2026, 9, 28)
    before = week_load(next(w for w in weeks if w.start == nxt), t)
    tp.finalize_weeks(weeks, g, normal_week=200)
    low = week_load(next(w for w in weeks if w.start == nxt), t)
    assert low < 0.9 * before
    tp.finalize_weeks(weeks, g, normal_week=900)
    w = next(w for w in weeks if w.start == nxt)
    back = week_load(w, t)
    assert back >= 0.95 * min(before, w.tss_target), (before, low, back, w.tss_target)
    assert all(s.duration_min <= tp._date_cap_min(g, s.day, {})[0] for s in w.sessions
               if s.session_type != "rest")


def test_9_9_a_sync_never_raises_a_week_above_its_built_budget():
    """The guard tracks down and back up, never above the budget a week was
    built with -- not when the load would allow more, not when it allows a
    little more (the evaluator's mutant max(built, cap) passed every test)."""
    g, weeks = _owner_weeks()
    t = _dt.date(2026, 9, 21)
    clock.freeze(t)
    built = {w.start: w.tss_target for w in weeks}
    tp.finalize_weeks(weeks, g, normal_week=900)                      # cap far above
    assert all(w.tss_target <= built[w.start] for w in weeks), \
        [(w.start, built[w.start], w.tss_target) for w in weeks]
    # a lowered week whose cap now sits just above its built budget
    w = next(w for w in weeks if w.start == _dt.date(2026, 9, 28) and not w.is_stepback)
    b = built[w.start]
    w.tss_target, w.guard_lowered = b - 40, {"from": b, "to": b - 40, "when": "2026-09-20",
                                            "carried": 300}
    clock.freeze(w.start)               # its Monday: the load carried in is the one handed
    carried = b * 1.03 / 1.3            # the cap: 3 % over the built budget
    tp.finalize_weeks(weeks, g, normal_week=carried)
    assert w.tss_target == b and not w.guard_lowered, (b, w.tss_target, w.guard_lowered)
