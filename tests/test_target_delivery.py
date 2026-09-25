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


def test_9_1_the_fill_stops_at_the_budget_and_skips_unload_weeks():
    clock.freeze(_dt.date(2026, 9, 21))
    g = F.owner("continuous")
    w, lib = _fill_week()
    load0 = week_load(w, clock.today())
    w.tss_target = load0 + 30
    tp._fill_easy_to_budget(w, g, lib)
    assert load0 + 20 <= week_load(w, clock.today()) <= load0 + 40
    w, lib = _fill_week()
    w.is_stepback = True
    tp._fill_easy_to_budget(w, g, lib)
    assert week_load(w, clock.today()) == load0


@pytest.mark.parametrize("case", [
    # sweep plans that read over 1.3 x on d190fa82
    ("hi-ftp-event", "2026-10-07"), ("event20-zeroday", "2026-09-15"),
    ("ftpvo2-threshold", "2026-09-14"),
    # ...and with the weeks filled but the rows not held to the guard
    ("owner-cont", "2026-09-15"), ("hi-ftp-event", "2026-09-21"),
    ("event20-zeroday", "2026-09-24")])
def test_9_2_the_guard_holds_on_the_sweep(case, gate_seed):
    """The ramp budgets a row within 1.3 x the load it counted; the passes
    after it cut rows it had counted, so the load carried into a later row
    was less than the ramp's and a row holding its budget read over the
    guard. The final step holds each row to the guard on the rows as
    planned (the auditor's rule)."""
    name, day = case
    t = _dt.date.fromisoformat(day)
    g, ctl, rwt, ftp = {
        "hi-ftp-event": (sweep_goal("event", t, rest=[0], hpw=15,
                                    daily={d: 3.0 for d in range(1, 7)}, race_in=16), 90, 750, 600),
        "event20-zeroday": (sweep_goal("event", t, rest=[0], hpw=10, race_in=20,
                                       daily={1: 1.5, 2: 0.0, 3: 1.5, 4: 1.0, 5: 4.0, 6: 3.0}),
                            50, 380, 247),
        "ftpvo2-threshold": (sweep_goal("ftp_vo2max", t, rest=[0], hpw=8, weeks=12,
                                        distribution="threshold"), 50, 380, 50),
        "owner-cont": (sweep_goal("continuous", t, rest=[4], hpw=12.5,
                                  daily={0: 2.0, 1: 2.0, 2: 2.0, 3: 2.0, 5: 3.0, 6: 2.5}),
                       55, 380, 247)}[name]
    weeks = sweep_gen(g, t, ctl, rwt, ftp)
    assert not pi.check_acwr(weeks, tp.athlete_weekly_load(ctl, rwt), today=t)


# ── 9.4 A lengthened slot serves a file that fits it ─────────────────────

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
