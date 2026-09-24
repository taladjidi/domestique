"""Planner final-step contract (notes/planner-finalize-contract.md), cause 6.

One test class per assertion. Each test was seen to fail on 02a0f514 before
its fix landed; the commit that fixed it records the mutation.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── 6.11 Same inputs, same plan ─────────────────────────────────────────

_APP_CHILD = r'''
import os, sys, json, datetime as dt
sys.path.insert(0, os.path.join(os.environ["PROBE_ROOT"], "src"))
import clock
from migrate_profiles import migrate_to_profiles; migrate_to_profiles()
from profile_manager import ProfileManager; ProfileManager.get()
import db; db.init_db()
import app as app_module, training_planner as tp
tp.get_today_metrics = lambda: {}; app_module.get_today_metrics = lambda: {}
from fastapi.testclient import TestClient
c = TestClient(app_module.app)
clock.freeze(dt.date(2026, 9, 16))
daily = {"0": 2.0, "1": 2.0, "2": 2.0, "3": 2.0, "4": 0, "5": 3.0, "6": 2.5}
body = {"goal": "event", "event_date": "2026-12-13", "event_km": 160, "event_climb": 2000,
        "rest_days": [4], "daily_availability": daily, "hours_per_week": 12.5,
        "max_weekday": 2.0, "max_weekend": 3.0}
path = tp.PLAN_DIR / "current_plan.json"
def weeks():
    p = json.loads(path.read_text())
    return [[[s["day"], s["session_type"], s.get("duration_min"), s.get("tss_estimate"),
              s.get("zwo_file")] for s in w["sessions"]] for w in p["weeks"]]
out = {}
assert c.post("/api/plan/generate", json=body).status_code == 200
out["generate"] = weeks()
stored = path.read_text()
for i in (1, 2):
    path.write_text(stored)
    assert c.post("/api/plan/regenerate", json={}).status_code == 200
    out[f"regenerate{i}"] = weeks()
print("@@@" + json.dumps(out))
'''


def _app_child(hash_seed: str) -> dict:
    home = tempfile.mkdtemp(prefix="dq-611-")
    env = {**os.environ, "HOME": home, "PYTHONHASHSEED": hash_seed,
           "DOMESTIQUE_NO_NET": "1", "PROBE_ROOT": str(ROOT),
           "DOMESTIQUE_ALLOWED_HOSTS": "testserver", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("DOMESTIQUE_HOME", None)
    out = subprocess.run([sys.executable, "-c", _APP_CHILD], capture_output=True,
                         text=True, env=env, timeout=600)
    line = next((ln for ln in out.stdout.splitlines() if ln.startswith("@@@")), None)
    assert line, out.stdout[-1500:] + out.stderr[-1500:]
    return json.loads(line[3:])


def test_same_inputs_same_plan_across_processes():
    """Two processes, identical inputs, the clock frozen: the same plan from
    /api/plan/generate, and the same plan from /api/plan/regenerate run twice
    on the same stored plan. On 02a0f514 both endpoints salted the planner
    with time.time_ns(), and this week's planned TSS came out 192..249."""
    a, b = _app_child("1"), _app_child("2")
    assert a["generate"] == b["generate"]
    assert a["regenerate1"] == a["regenerate2"] == b["regenerate1"]


# ── 6.10 The auditor sees what it missed ─────────────────────────────────

import datetime as _dt  # noqa: E402

sys.path.insert(0, str(ROOT / "src"))
import training_planner as tp  # noqa: E402
import plan_invariants as pi  # noqa: E402

_MON = _dt.date(2026, 9, 7)
VO2 = "vo2max_5x3min-3min_115pct_60min.zwo"        # classified vo2max
Z2 = "z2_steady_68pct_90min.zwo"                   # classified endurance


def _d(i):
    return _MON + _dt.timedelta(days=i)


def _s(i, t="z2", dur=60, tss=45, f=Z2, **kw):
    s = tp.PlannedSession(day=_d(i), day_name=_d(i).strftime("%a"), session_type=t,
                          duration_min=dur, tss_estimate=tss, description="x", zwo_file=f)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _w(sessions, n=1, first=0, last=6, phase="build1", target=400, stepback=False):
    return tp.PlannedWeek(week_num=n, start=_d(first), end=_d(last), phase=phase,
                          tss_target=target, is_stepback=stepback, sessions=sessions)


def _goal(**kw):
    base = dict(goal_type="continuous", rest_days=[6], available_days=[0, 1, 2, 3, 4, 5],
                daily_max_hours={d: 2.0 for d in range(6)}, hours_per_week=10,
                max_weekday_hours=2.0, max_weekend_hours=2.0, plan_weeks=4)
    base.update(kw)
    return tp.Goal(**base)


def _week_ok(n=1, first=0, phase="build1", load=1.0):
    return _w([_s(first + 0, "vo2max", 60, round(80 * load), VO2),
               _s(first + 1, "z2", 90, round(60 * load)),
               _s(first + 3, "vo2max", 60, round(80 * load), VO2),
               _s(first + 5, "z2", 90, round(60 * load))],
              n=n, first=first, last=first + 6, phase=phase)


def _rules(weeks, g=None, **kw):
    return [v.rule for v in pi.audit(weeks, g or _goal(), **kw)]


NEW_RULES = ("bad_number", "race_missing", "taper_heavier", "hard_slot_content",
             "missing_file", "acwr")


def test_legal_plan_fires_none_of_the_new_rules():
    weeks = [_week_ok(1, 0), _week_ok(2, 7), _week_ok(3, 14, "taper", 0.6)]
    hits = _rules(weeks)
    assert not [r for r in hits if r in NEW_RULES], hits


def test_nan_and_negative_load_fire():
    for bad in (float("nan"), -5.0, float("inf")):
        wk = _week_ok()
        wk.sessions[1].tss_estimate = bad
        assert "bad_number" in _rules([wk]), bad
    wk = _week_ok()
    wk.sessions[1].duration_min = -5
    assert "bad_number" in _rules([wk])


def test_race_missing_from_its_day_fires():
    race = _d(13)
    g = _goal(goal_type="event", target_date=race, rest_days=[], available_days=[])
    weeks = [_week_ok(1, 0), _week_ok(2, 7)]
    weeks[1].sessions.append(_s(13, "recovery", 60, 50, "", is_race=True))
    assert "race_missing" not in _rules(weeks, g)
    weeks[1].sessions[-1].is_race = False
    assert "race_missing" in _rules(weeks, g)
    # A B race in goal.events counts the same.
    g.events = [{"date": _d(5).isoformat(), "priority": "B"}]
    weeks[1].sessions[-1].is_race = True
    assert _rules(weeks, g).count("race_missing") == 1


def test_taper_heavier_than_last_build_fires():
    ok = [_week_ok(1, 0), _week_ok(2, 7), _week_ok(3, 14, "taper", 0.6)]
    assert "taper_heavier" not in _rules(ok)
    heavy = [_week_ok(1, 0), _week_ok(2, 7), _week_ok(3, 14, "taper", 1.2)]
    assert "taper_heavier" in _rules(heavy)


def test_hard_slot_serving_easy_content_fires():
    wk = _week_ok()
    assert "hard_slot_content" not in _rules([wk])
    wk.sessions[0].zwo_file = Z2
    assert "hard_slot_content" in _rules([wk])


def test_training_session_without_a_file_fires():
    wk = _week_ok()
    wk.sessions.append(_s(4, "rest", 0, 0, "", description="Rest"))
    wk.sessions.append(_s(2, "recovery", 60, 40, "", is_race=True))
    assert "missing_file" not in _rules([wk])        # rest and a race need no file
    for f in ("", "nope_not_in_library.zwo"):
        bad = _week_ok()
        bad.sessions[1].zwo_file = f
        assert "missing_file" in _rules([bad]), f


def test_acwr_over_the_sweet_spot_is_reported_from_the_rides():
    rides = [{"date": (_MON - _dt.timedelta(days=i)).isoformat(), "tss": 40.0}
             for i in range(1, 71)]                   # 280 TSS a week for ten weeks
    steady = [_week_ok(1, 0, load=280 / 280), _week_ok(2, 7, load=1.0)]
    assert "acwr" not in _rules(steady, rides=rides, today=_MON)
    spike = [_week_ok(1, 0, load=1.8)]
    assert "acwr" in _rules(spike, rides=rides, today=_MON)
    # Too little history to say what the rider carries: no report.
    assert "acwr" not in _rules(spike, rides=rides[:20], today=_MON)


def test_daily_cap_honours_per_date_overrides():
    wk = _week_ok()
    wk.sessions[1].duration_min = 170                 # Tuesday, weekday cap 2 h
    three_h = {_d(1).isoformat(): 3.0}
    assert "daily_duration_cap" not in _rules([wk], overrides=three_h)
    assert "daily_duration_cap" in _rules([wk], overrides={_d(1).isoformat(): 1.0})
    # A rest day the rider opened for 3 h is a training day that date.
    wk.sessions.append(_s(6, "long_z2", 170, 110))
    assert "rest_days" not in _rules([wk], overrides={**three_h, _d(6).isoformat(): 3.0})
    assert "rest_days" in _rules([wk], overrides=three_h)
    # A date zeroed in the calendar holds nothing.
    assert "rest_days" in _rules([wk], overrides={**three_h, _d(6).isoformat(): 3.0,
                                                  _d(3).isoformat(): 0.0})


# ── 6.1-6.9 The final step, on every entry point ─────────────────────────
#
# One rider per shape, the owner's own week (Friday off, 2 h weekdays, 3 h
# Saturday, 2.5 h Sunday). Every entry point is driven from the plan generate
# built on Monday 2026-09-14, on the Thursday of its third week.

import copy  # noqa: E402

import clock  # noqa: E402
import pytest  # noqa: E402

G0 = _dt.date(2026, 9, 14)                      # Monday
T3 = G0 + _dt.timedelta(days=17)                # Thursday of week 3
DAILY = {0: 2.0, 1: 2.0, 2: 2.0, 3: 2.0, 5: 3.0, 6: 2.5}
ATH = {"ftp": 247, "weight_kg": 72}


def owner(kind="event", **kw):
    g = tp.Goal(goal_type=kind, rest_days=[4], available_days=[0, 1, 2, 3, 5, 6],
                daily_max_hours=dict(DAILY), max_weekday_hours=2.0, max_weekend_hours=3.0,
                hours_per_week=12.5,
                target_date=(_dt.date(2026, 12, 13) if kind == "event" else None),
                event_km=(160 if kind == "event" else 0),
                event_climb_m=(2000 if kind == "event" else 0))
    for k, v in kw.items():
        setattr(g, k, v)
    return g


def gen(goal, today=G0, **kw):
    clock.freeze(today)
    return tp.generate_plan(goal, current_ctl=55, recent_weekly_tss=380, athlete=ATH, **kw)[1]


def ride_days(first, last, tss=60.0):
    out, d = [], first
    while d <= last:
        if d.weekday() != 4:
            out.append({"date": d.isoformat(), "tss": tss})
        d += _dt.timedelta(days=1)
    return out


@pytest.fixture(autouse=True)
def _clock():
    yield
    clock.unfreeze()


def entry_points(kind, base, t, goal_fn, rides=None):
    """Every planner entry point, driven from ``base`` on day ``t``. Yields
    (name, weeks). The goal each gets comes from goal_fn (fresh each time)."""
    rides = ride_days(t - _dt.timedelta(days=14), t - _dt.timedelta(days=1)) \
        if rides is None else rides

    def regen():
        g = goal_fn()
        g.plan_weeks = 0
        return tp.regenerate_from_today(g, copy.deepcopy(base), 55, activities=rides,
                                        athlete=ATH, recent_weekly_tss=380)[1]

    def recalc():
        g = goal_fn()
        g.plan_weeks = 0
        return tp.recalculate_plan(g, copy.deepcopy(base), 70, recent_activities=rides,
                                   athlete=ATH, recent_weekly_tss=380)[1]

    def refit():
        wk = copy.deepcopy(base)
        cur = next(w for w in wk if w.start <= t <= w.end)
        past = [s for s in cur.sessions if s.day < t and s.session_type != "rest"]
        if past and not any(s.status == "missed" and tp._session_is_hit(s) for s in past):
            past[-1].session_type, past[-1].status = "vo2max", "missed"
        return tp.refit_remaining_week(goal_fn(), wk, t)[0]

    def refc():
        series = {t + _dt.timedelta(days=i): -35.0 for i in range(-14, 140)}
        return tp.reforecast(goal_fn(), copy.deepcopy(base), tsb_series=series,
                             recent_activities=rides)[0]

    drivers = [("regenerate", regen), ("recalculate", recalc), ("refit", refit),
               ("reforecast", refc)]
    if kind == "continuous":
        drivers.append(("extend", lambda: tp.extend_continuous_plan(
            goal_fn(), copy.deepcopy(base), 55, recent_activities=rides, athlete=ATH,
            recent_weekly_tss=380)[1]))
    for name, fn in drivers:
        clock.freeze(t)
        yield name, fn()


def pending_ahead(weeks, t):
    return [s for w in weeks for s in w.sessions
            if s.day >= t and s.session_type != "rest" and not s.is_race
            and s.status == "pending"]


# 6.1 -------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_6_1_a_zeroed_date_holds_nothing_after_any_entry_point(kind):
    base = gen(owner(kind))
    zero = [s.day for s in pending_ahead(base, T3 + _dt.timedelta(days=1))][:3]
    cal = {d.isoformat(): 0.0 for d in zero}

    def goal_fn():
        g = owner(kind)
        g._calendar = dict(cal)
        return g
    seen = []
    for name, weeks in entry_points(kind, base, T3, goal_fn):
        seen.append(name)
        on_zero = [(name, s.day, s.session_type) for s in pending_ahead(weeks, T3)
                   if s.day in zero]
        assert not on_zero, on_zero
    assert len(seen) >= 4


def test_6_1_generate_honours_an_unavailable_period():
    lo, hi = G0 + _dt.timedelta(days=8), G0 + _dt.timedelta(days=10)
    weeks = gen(owner(), unavailable_periods=[(lo, hi)])
    assert not [s for s in pending_ahead(weeks, G0) if lo <= s.day <= hi]


# 6.2 -------------------------------------------------------------------

def _in_library():
    return {r["File"] for r in tp.load_workout_library()}


@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_6_2_every_session_serves_a_library_file(kind):
    lib = _in_library()
    base = gen(owner(kind))
    assert all(s.zwo_file in lib for s in pending_ahead(base, G0))
    for name, weeks in entry_points(kind, base, T3, lambda: owner(kind)):
        bad = [(name, s.day, s.session_type, s.zwo_file) for s in pending_ahead(weeks, T3)
               if s.zwo_file not in lib]
        assert not bad, bad


def test_6_2_the_recovery_ramp_serves_files_on_monday_weeks():
    """No rides for three weeks: regenerate builds the recovery ramp. On
    02a0f514 its sessions had no file (815 in the reviewer's sweep) and its
    rows ran Thursday to Wednesday."""
    lib = _in_library()
    base = gen(owner())
    clock.freeze(T3)
    _, weeks, info = tp.regenerate_from_today(owner(plan_weeks=0), copy.deepcopy(base), 40,
                                              activities=[], athlete=ATH,
                                              recent_weekly_tss=380)
    assert info["recovery_ramp_weeks"] > 0
    assert not [s for s in pending_ahead(weeks, T3) if s.zwo_file not in lib]
    ramp = [w for w in weeks if w.phase in ("recon", "recovery_ramp")]
    assert ramp and all(w.end.weekday() == 6 and w.start.weekday() == 0 for w in ramp), \
        [(w.start, w.end) for w in ramp]


# 6.3 -------------------------------------------------------------------

def _stamp(weeks, t):
    """Mark the current week's days before t: done, dismissed, missed, and the
    rest left pending."""
    cur = next(w for w in weeks if w.start <= t <= w.end)
    past = [s for s in cur.sessions if s.day < t]
    for s, st in zip(past, ("done", "dismissed", "missed")):
        s.status = st
        if st == "missed":
            s.session_type = "vo2max"       # what triggers refit
    return {s.day: (s.session_type, s.duration_min, s.tss_estimate, s.zwo_file, s.status)
            for w in weeks for s in w.sessions if s.day < t}


@pytest.mark.parametrize("rides", ["ridden", "absent"])
def test_6_3_days_before_today_are_a_record(rides):
    """'absent' has no rides: regenerate takes the recovery-ramp path, which
    on 02a0f514 dropped Monday-Wednesday, done and dismissed alike."""
    base = gen(owner())
    rec = _stamp(base, T3)
    rs = None if rides == "ridden" else []
    for name, weeks in entry_points("event", base, T3, owner, rides=rs):
        got = {s.day: (s.session_type, s.duration_min, s.tss_estimate, s.zwo_file, s.status)
               for w in weeks for s in w.sessions if s.day < T3}
        assert got == rec, (name, {d: (rec.get(d), got.get(d)) for d in set(rec) | set(got)
                                   if rec.get(d) != got.get(d)})


# 6.4 -------------------------------------------------------------------

@pytest.mark.parametrize("days_out", [7, 9, 12, 16])
def test_6_4_a_micro_plan_taper_is_capped(days_out):
    """A race nine days out on 02a0f514: a 486 TSS taper against a 228 target,
    because no full build week came before it to anchor the cap."""
    g = owner(target_date=G0 + _dt.timedelta(days=days_out),
              plan_weeks=max(4, -(-days_out // 7)))
    weeks = gen(g)
    builds = [w for w in weeks if w.phase != "taper" and not w.is_stepback
              and (w.end - w.start).days >= 6]
    for w in weeks:
        if w.phase != "taper":
            continue
        span = (w.end - w.start).days + 1
        load = sum(s.tss_estimate for s in w.sessions if s.session_type != "rest" and not s.is_race)
        assert load <= 380 * tp.TAPER_FRACS[0] * span / 7 * 1.05 + 1, (w.start, load)
        if builds:
            last = sum(s.tss_estimate for s in builds[-1].sessions
                       if s.session_type != "rest" and not s.is_race)
            assert load * 7 / span <= last * 1.05 + 1


# 6.5 -------------------------------------------------------------------

def _monfri():
    return tp.Goal(goal_type="continuous", rest_days=[5, 6], available_days=[0, 1, 2, 3, 4],
                   daily_max_hours={d: 1.0 for d in range(5)}, hours_per_week=5)


@pytest.mark.parametrize("rider", ["owner", "monfri-1h"])
def test_6_5_extend_holds_appended_weeks_to_their_target(rider):
    """The weekly auto-recalc of a continuous plan (recalculate_plan ->
    extend). On 02a0f514 the reviewer's extend_q found 11 of 104 appended
    weeks over target x 1.05, up to 1.26x."""
    goal_fn, ctl, rwt = ((lambda: owner("continuous")), 55, 380) if rider == "owner" \
        else (_monfri, 30, 200)
    over = []
    for b in (0, 2, 4, 6):
        b0 = G0 + _dt.timedelta(days=b)
        clock.freeze(b0)
        base = tp.generate_plan(goal_fn(), seed_salt=b, current_ctl=ctl,
                                recent_weekly_tss=rwt, athlete=ATH)[1]
        old = {w.start for w in base}
        for off in (7, 10, 14, 21):
            t = b0 + _dt.timedelta(days=off)
            clock.freeze(t)
            rs = [r for r in ride_days(t - _dt.timedelta(days=14), t - _dt.timedelta(days=1))
                  if _dt.date.fromisoformat(r["date"]).weekday() in goal_fn().available_days]
            _, weeks, _ = tp.recalculate_plan(goal_fn(), copy.deepcopy(base), ctl,
                                              recent_activities=rs, athlete=ATH,
                                              recent_weekly_tss=rwt)
            for w in weeks:
                if w.start in old or (w.end - w.start).days < 6:
                    continue
                load = sum(s.tss_estimate for s in w.sessions if s.session_type != "rest")
                if load > w.tss_target * 1.05 + 1:
                    over.append((str(t), w.week_num, load, w.tss_target))
    assert not over, over


# 6.6 -------------------------------------------------------------------

def test_6_6_a_b_race_beyond_the_horizon_is_marked_once_extend_reaches_it():
    race = G0 + _dt.timedelta(days=41)                  # a Sunday, week 6
    goal_fn = lambda: owner("continuous", events=[tp.TargetEvent(  # noqa: E731
        date=race, priority="B", name="club", event_km=90, event_climb_m=900)])
    weeks = gen(goal_fn())
    assert weeks[-1].end < race
    for off in (8, 15):
        t = G0 + _dt.timedelta(days=off)
        clock.freeze(t)
        weeks = tp.extend_continuous_plan(goal_fn(), weeks, 55, athlete=ATH,
                                          recent_weekly_tss=380)[1]
    days = {s.day: s for w in weeks for s in w.sessions}
    assert race in days and days[race].is_race
    assert days[race - _dt.timedelta(days=1)].is_opener \
        or days[race - _dt.timedelta(days=1)].session_type == "rest"
    for d in (race - _dt.timedelta(days=2), race - _dt.timedelta(days=3)):
        assert not tp._session_is_hit(days[d]), (d, days[d].session_type)


# 6.7 -------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_6_7_a_heavy_monday_to_wednesday_counts_against_the_week(kind):
    """450 TSS ridden Monday-Wednesday, then generate / regenerate on
    Thursday: on 02a0f514 the calendar week came to 583 against 494 (a floor
    added a VO2max day after the budget was set)."""
    t = G0 + _dt.timedelta(days=3)
    rides = [{"date": (G0 + _dt.timedelta(days=i)).isoformat(), "tss": 150.0} for i in range(3)]
    clock.freeze(t)
    ph, weeks = tp.generate_plan(owner(kind), current_ctl=55, recent_weekly_tss=380,
                                 athlete=ATH, activities=rides)
    w1 = weeks[0]
    gross = w1.tss_target * 7 / ((w1.end - w1.start).days + 1)
    ahead = sum(s.tss_estimate for s in w1.sessions if s.session_type != "rest" and not s.is_race)
    assert 450 + ahead <= gross * 1.05 + 1, (ahead, gross)
    base = gen(owner(kind))
    clock.freeze(t)
    _, weeks, _ = tp.regenerate_from_today(owner(kind, plan_weeks=0), copy.deepcopy(base), 55,
                                           activities=rides, athlete=ATH, recent_weekly_tss=380)
    cur = next(w for w in weeks if w.start <= t <= w.end)
    ahead = sum(s.tss_estimate for s in cur.sessions
                if s.day >= t and s.session_type != "rest" and not s.is_race)
    assert 450 + ahead <= float(cur.tss_target) * 1.05 + 1, (ahead, cur.tss_target)


# 6.8 -------------------------------------------------------------------

def _load_matches(weeks, t):
    rows = {r["File"]: r for r in tp.load_workout_library()}
    bad = []
    for s in pending_ahead(weeks, t):
        r = rows[s.zwo_file]
        load = float(r["TSS"]) * s.duration_min / float(r["Duration(min)"])
        if abs(s.tss_estimate - load) > (tp._MATCH_BUDGET_TOLERANCE - 1) * load + 1:
            bad.append((s.day, s.session_type, s.duration_min, s.tss_estimate, round(load), s.zwo_file))
        m = tp._DESC_MIN.search(s.description or "")
        if m and int(m.group(1)) != s.duration_min:
            bad.append((s.day, "description", s.description, s.duration_min))
    return bad


@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_6_8_tss_and_minutes_are_the_served_files(kind):
    """The Rønnestad floor swapped files and kept the old TSS (261 mismatches
    in the reviewer's sweep); descriptions kept a length the clamp had cut."""
    base = gen(owner(kind))
    assert not _load_matches(base, G0)
    for name, weeks in entry_points(kind, base, T3, lambda: owner(kind)):
        bad = _load_matches(weeks, T3)
        assert not bad, (name, bad[:5])


# 6.9 -------------------------------------------------------------------

def test_6_9_the_race_eve_opener_never_lands_on_a_rest_day():
    """Race on Saturday, Friday declared rest (OWN-6)."""
    race = _dt.date(2026, 12, 12)
    weeks = gen(owner(target_date=race))
    eve = next(s for w in weeks for s in w.sessions if s.day == race - _dt.timedelta(days=1))
    assert eve.session_type == "rest" and not eve.is_opener


def test_6_9_an_ftp_test_is_its_file_length():
    rows = {r["File"]: r for r in tp.load_workout_library()}
    tests = [s for w in gen(owner("continuous", daily_max_hours={d: 3.0 for d in DAILY}))
             for s in w.sessions if s.session_type == "ftp_test"]
    tests += [s for w in gen(owner("ftp", target_date=G0 + _dt.timedelta(weeks=10)))
              for s in w.sessions if s.session_type == "ftp_test"]
    assert tests
    for s in tests:
        assert s.duration_min == round(float(rows[s.zwo_file]["Duration(min)"])), \
            (s.day, s.duration_min, s.zwo_file)


def test_6_9_a_zero_hour_day_that_is_not_a_rest_day_holds_nothing():
    g = owner(daily_max_hours={**DAILY, 2: 0.0})        # Wednesday 0 h, not a rest day
    weeks = gen(g)
    assert not [s for s in pending_ahead(weeks, G0) if s.day.weekday() == 2]
    for name, w2 in entry_points("event", weeks, T3,
                                 lambda: owner(daily_max_hours={**DAILY, 2: 0.0})):
        assert not [s for s in pending_ahead(w2, T3) if s.day.weekday() == 2], name


# 6.1 through the app: re-saving the same calendar repairs a plan that
# violates it, and mark-unavailable clears its period at once.

def _app_plan(tmp_path, monkeypatch, today):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    import app as app_module
    clock.freeze(today)
    weeks = gen(owner(), today=today)
    clock.freeze(today)
    plan = {"goal": tp.goal_to_dict(owner()), "phases": [],
            "weeks": [tp.week_to_dict(w) for w in weeks]}
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    (tmp_path / "current_plan.json").write_text(json.dumps(plan))
    p = patch("db.query_activities", return_value=[])
    p.start()
    return TestClient(app_module.app), plan, p


def _sessions_on(tmp_path, days):
    plan = json.loads((tmp_path / "current_plan.json").read_text())
    return [(s["day"], s["session_type"]) for w in plan["weeks"] for s in w["sessions"]
            if s["day"] in days and s["session_type"] != "rest"]


def test_6_1_re_saving_the_same_calendar_repairs_the_plan(tmp_path, monkeypatch):
    client, plan, p = _app_plan(tmp_path, monkeypatch, G0)
    try:
        days = [s["day"] for w in plan["weeks"][1:3] for s in w["sessions"]
                if s["session_type"] != "rest"][:3]
        avail = {d: {"hours": 0, "type": "holiday"} for d in days}
        # The calendar is already saved, and the plan still trains on it: what
        # a plan written by 02a0f514's regenerate looks like.
        plan["availability"] = avail
        (tmp_path / "current_plan.json").write_text(json.dumps(plan))
        assert _sessions_on(tmp_path, days)
        r = client.post("/api/plan/save-availability", json={"availability": avail})
        assert r.status_code == 200, r.text
        assert _sessions_on(tmp_path, days) == []
    finally:
        p.stop()


def test_6_1_mark_unavailable_clears_its_period(tmp_path, monkeypatch):
    client, plan, p = _app_plan(tmp_path, monkeypatch, G0)
    try:
        lo, hi = G0 + _dt.timedelta(days=8), G0 + _dt.timedelta(days=12)
        days = [(lo + _dt.timedelta(days=i)).isoformat() for i in range(5)]
        assert _sessions_on(tmp_path, days)
        r = client.post("/api/plan/mark-unavailable",
                        json={"start": lo.isoformat(), "end": hi.isoformat()})
        assert r.status_code == 200, r.text
        assert _sessions_on(tmp_path, days) == []
    finally:
        p.stop()


def test_6_1_a_closed_date_never_reaches_the_calendar_push():
    """A plan stored before the final step existed can still carry a session
    on a date the calendar closes; the push leaves it out, and a stale event
    for that slot is swept (neither pushed nor protected)."""
    from unittest.mock import MagicMock
    import icu_calendar_push as icp
    pm = MagicMock()
    pm.prefs, pm._athlete = {}, {"target_mode": "power"}
    pm.lthr_is_set, pm.max_hr, pm.lthr = True, 190, 170
    today = _dt.date.today()
    closed, open_ = [(today + _dt.timedelta(days=i)).isoformat() for i in (2, 3)]
    plan = {"weeks": [{"sessions": [
        {"day": closed, "session_type": "z2", "zwo_file": "", "duration_min": 60,
         "tss_estimate": 45},
        {"day": open_, "session_type": "threshold", "zwo_file": "", "duration_min": 60,
         "tss_estimate": 90}]}],
        "availability": {closed: {"hours": 0, "type": "holiday"}}}
    events, skipped, broken = icp._desired_events(pm, plan, today, 14, "pidX")
    assert [s["day"] for s in skipped] == [open_]      # the open day is still wanted
    assert f"domestique:pidX:{closed}:0" not in broken


@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_the_final_step_is_a_fixpoint(kind):
    """Every sync runs it (reforecast_dict): a second run on its own output
    changes nothing, or each sync would churn the rider's calendar."""
    for name, weeks in entry_points(kind, gen(owner(kind)), T3, lambda: owner(kind)):
        clock.freeze(T3)
        again = tp.finalize_weeks(weeks, owner(kind),
                                  rides=ride_days(T3 - _dt.timedelta(days=14),
                                                  T3 - _dt.timedelta(days=1)),
                                  normal_week=380)
        assert again == set(), (name, sorted(again)[:5])


# 6.1, the review of 2026-09-24: one reading of the calendar on every path.

def test_6_1_the_rest_button_holds_through_regenerate_and_sync(tmp_path, monkeypatch):
    """The calendar's REST button stores {hours: 0, type: "rest"}. Regenerate
    read blocks only and refilled the day, the sync's reforecast read the
    whole calendar and rested it again: rest / threshold 59 / rest ... (V1)."""
    client, plan, p = _app_plan(tmp_path, monkeypatch, G0)
    try:
        days = [s["day"] for w in plan["weeks"][1:3] for s in w["sessions"]
                if s["session_type"] != "rest"][:2]
        avail = {d: {"hours": 0, "type": "rest"} for d in days}
        assert client.post("/api/plan/save-availability",
                           json={"availability": avail}).status_code == 200
        assert _sessions_on(tmp_path, days) == []
        for _ in range(2):
            assert client.post("/api/plan/regenerate", json={}).status_code == 200
            assert _sessions_on(tmp_path, days) == []
            assert client.post("/api/plan/reforecast").status_code == 200
            assert _sessions_on(tmp_path, days) == []
    finally:
        p.stop()


def test_6_1_generate_keeps_the_unavailable_periods(tmp_path, monkeypatch):
    client, plan, p = _app_plan(tmp_path, monkeypatch, G0)
    try:
        lo, hi = G0 + _dt.timedelta(days=8), G0 + _dt.timedelta(days=9)
        days = [lo.isoformat(), hi.isoformat()]
        assert client.post("/api/plan/mark-unavailable",
                           json={"start": days[0], "end": days[1]}).status_code == 200
        body = {"goal": "event", "event_date": "2026-12-13", "event_km": 160,
                "event_climb": 2000, "rest_days": [4], "hours_per_week": 12.5,
                "daily_availability": {str(k): v for k, v in {**DAILY, 4: 0}.items()},
                "max_weekday": 2.0, "max_weekend": 3.0}
        r = client.post("/api/plan/generate", json=body)
        assert r.status_code == 200, r.text
        assert _sessions_on(tmp_path, days) == []
        stored = json.loads((tmp_path / "current_plan.json").read_text())
        assert stored.get("unavailable_periods")
    finally:
        p.stop()


@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_6_1_a_no_change_recalculate_still_runs_the_final_step(kind):
    """recalculate_plan's <= 8 % deviation return, and extend's horizon_full,
    skipped the final step: a date zeroed after the plan was built stayed
    filled (V2)."""
    base = gen(owner(kind))
    t = G0 + _dt.timedelta(days=1)
    zero = [s.day for s in pending_ahead(base, t)][:3]
    g = owner(kind, plan_weeks=0)
    g._calendar = {d.isoformat(): 0.0 for d in zero}
    clock.freeze(t)
    _, weeks, info = tp.recalculate_plan(g, copy.deepcopy(base), 55, athlete=ATH,
                                         recent_weekly_tss=380)
    assert not [s for s in pending_ahead(weeks, t) if s.day in zero], info["action"]


# M1 (evaluation of b7be0395, the owner's data): the week's ridden load counts
# each ride once, and only rides.

def _act(aid, day, hhmm, secs, sport, tss, power=True):
    start = f"{day}T{hhmm}:00"
    return {"id": aid, "date": day, "sport": sport, "tss": float(tss), "duration_sec": secs,
            "avg_power": 180.0 if power else None,
            "raw_json": json.dumps({"start_date_local": start, "elapsed_time": secs,
                                    "moving_time": secs, "type": sport})}


def _owner_week_activities(monday):
    """The owner's week of 2026-09-21: Monday's Bryton ride (205) and the
    Garmin band's HR-load 'Workout' of the same window (252); Wednesday's
    Bryton ride (18) and the band's 'VirtualRide' of the same ride (13)."""
    mon, wed = monday.isoformat(), (monday + _dt.timedelta(days=2)).isoformat()
    return [_act("i188888193", mon, "12:39", 13437, "Ride", 205),
            _act("i189048003", mon, "12:27", 15449, "Workout", 252, power=False),
            _act("i189542797", wed, "11:30", 5062, "Ride", 18),
            _act("i189708913", wed, "11:30", 6572, "VirtualRide", 13, power=False)]


def test_m1_the_ridden_load_counts_each_ride_once():
    acts = _owner_week_activities(G0)
    assert tp._completed_tss_in(acts, G0, G0 + _dt.timedelta(days=6)) == 223.0
    # A walk or a run on its own is not a ride either (the owner's 09-19 walk).
    walk = _act("i188418356", (G0 + _dt.timedelta(days=5)).isoformat(), "10:00", 3600,
                "Walk", 6, power=False)
    assert tp._completed_tss_in(acts + [walk], G0, G0 + _dt.timedelta(days=6)) == 223.0
    # Of two recordings of one ride, the one a session was matched to counts.
    assert tp._completed_tss_in(acts, G0, G0 + _dt.timedelta(days=6),
                                matched_ids={"i189708913"}) == 218.0


@pytest.mark.parametrize("kind", ["event", "continuous"])
def test_m1_a_duplicate_recording_does_not_rest_the_rest_of_the_week(kind):
    """On b7be0395 the owner's first sync counted 488 against a 479 target and
    rested Thursday and Friday; the calendar push then deleted both."""
    base = gen(owner(kind))
    t = G0 + _dt.timedelta(days=3)
    dup = _owner_week_activities(G0)
    clean = [a for a in dup if a["id"] in ("i188888193", "i189542797")]

    def ahead(acts):
        plan = {"goal": tp.goal_to_dict(owner(kind)), "weeks": [tp.week_to_dict(w) for w in base]}
        clock.freeze(t)
        tp.reforecast_dict(plan, tsb_series={}, recent_activities=acts)
        return [(s["day"], s["session_type"], s["duration_min"]) for w in plan["weeks"]
                for s in w["sessions"] if t.isoformat() <= s["day"] <= "2026-09-20"]
    got = ahead(dup)
    assert got == ahead(clean)
    assert [x for x in got if x[1] != "rest"], got
