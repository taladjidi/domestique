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


_put_record = None


@pytest.fixture(autouse=True)
def _one_seam(ride_record):
    """Every test here reads the rider's rides through the one seam
    (conftest.ride_record): none unless it says, never its xdist worker's
    ride store."""
    global _put_record
    _put_record = ride_record
    yield
    _put_record = None


def record(rides):
    """The rider's rides for the planner (the one seam); returns them."""
    _put_record(rides)
    return rides


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
        # the rider's load, as production reads it from the ride archive (9.9)
        return tp.refit_remaining_week(goal_fn(), wk, t, recent_weekly_tss=380)[0]

    def refc():
        series = {t + _dt.timedelta(days=i): -35.0 for i in range(-14, 140)}
        return tp.reforecast(goal_fn(), copy.deepcopy(base), tsb_series=series,
                             recent_activities=rides, recent_weekly_tss=380)[0]

    drivers = [("regenerate", regen), ("recalculate", recalc), ("refit", refit),
               ("reforecast", refc)]
    if kind == "continuous":
        drivers.append(("extend", lambda: tp.extend_continuous_plan(
            goal_fn(), copy.deepcopy(base), 55, recent_activities=rides, athlete=ATH,
            recent_weekly_tss=380)[1]))
    import ride_storage
    from unittest.mock import patch
    # the one seam: every driver, and the caller's own finalize, read ``rides``
    with patch.object(ride_storage, "ride_record", lambda: list(rides)):
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
                                              recent_activities=record(rs), athlete=ATH,
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
def test_6_7_a_heavy_monday_to_wednesday_counts_against_the_week(kind, ride_record):
    """450 TSS ridden Monday-Wednesday, then generate / regenerate on
    Thursday: on 02a0f514 the calendar week came to 583 against 494 (a floor
    added a VO2max day after the budget was set)."""
    t = G0 + _dt.timedelta(days=3)
    rides = [{"date": (G0 + _dt.timedelta(days=i)).isoformat(), "tss": 150.0} for i in range(3)]
    ride_record(rides)
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
def test_the_final_step_is_a_fixpoint(kind, ride_record):
    """Every sync runs it (reforecast_dict): a second run on its own output
    changes nothing, or each sync would churn the rider's calendar."""
    for name, weeks in entry_points(kind, gen(owner(kind)), T3, lambda: owner(kind)):
        clock.freeze(T3)
        again = tp.finalize_weeks(weeks, owner(kind), normal_week=380)
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
    # Of two recordings of one ride, the power meter's counts, even when the
    # session was matched to the band's (the plan's match on 09-23)...
    assert tp._completed_tss_in(acts, G0, G0 + _dt.timedelta(days=6),
                                matched_ids={"i189708913"}) == 223.0
    # ...and between two without power, the matched one.
    nop = [dict(a, avg_power=None) for a in acts]
    assert tp._completed_tss_in(nop, G0, G0 + _dt.timedelta(days=6),
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
        tp.reforecast_dict(plan, tsb_series={}, recent_activities=record(acts))
        return [(s["day"], s["session_type"], s["duration_min"]) for w in plan["weeks"]
                for s in w["sessions"] if t.isoformat() <= s["day"] <= "2026-09-20"]
    got = ahead(dup)
    assert got == ahead(clean)
    assert [x for x in got if x[1] != "rest"], got


# M2 (evaluation of b7be0395): a missed session is never destroyed.

def _missed_week_plan(kind="event"):
    """The stored plan on Thursday of week 1: Tuesday's session missed, and
    Saturday a rest day on a day the rider trains (a free slot)."""
    base = gen(owner(kind))
    plan = {"goal": tp.goal_to_dict(owner(kind)), "weeks": [tp.week_to_dict(w) for w in base]}
    tue, sat = "2026-09-15", "2026-09-19"
    for s in plan["weeks"][0]["sessions"]:
        if s["day"] == tue:
            s.update(session_type="z2", duration_min=60, tss_estimate=45, status="missed",
                     zwo_file="z2_steady_70pct_60min_v2.zwo")
        if s["day"] == sat:
            s.update(session_type="rest", duration_min=0, tss_estimate=0, zwo_file="",
                     description="Rest — recovery takes priority")
    return plan, tue


def _owed(plan, tue):
    """Where Tuesday's session is now: missed on its own day, or pending on
    the day it moved to."""
    ss = [s for w in plan["weeks"] for s in w["sessions"]]
    return ([s["day"] for s in ss if s["day"] == tue and s.get("status") == "missed"]
            + [s["day"] for s in ss if s.get("moved_from") == tue
               and s["session_type"] != "rest" and s.get("status") == "pending"])


def test_m2_a_missed_session_the_budget_will_not_carry_stays_missed():
    """600 TSS ridden Monday spends the week: on b7be0395 the auto-move put
    Tuesday's z2 on Saturday, the final step rested Saturday, and Tuesday was
    left a rest 'moved_from' -- the session existed nowhere."""
    import app as app_module
    plan, tue = _missed_week_plan()
    t = G0 + _dt.timedelta(days=3)
    clock.freeze(t)
    rides = [{"id": "r1", "date": G0.isoformat(), "sport": "Ride", "tss": 600.0}]
    for _ in range(2):                              # two syncs
        plan, _action, _info, _msg = app_module._apply_plan_update(
            plan, training={}, activities=record(rides), today=t)
        assert _owed(plan, tue), "the missed session is gone"
        # And no session anywhere: a day left "moved_from:X" is only a stub
        # when X holds the session, pending, moved from that day.
        ss = {s["day"]: s for w in plan["weeks"] for s in w["sessions"]}
        for d, s in ss.items():
            st = str(s.get("status") or "")
            if st.startswith("moved_from:"):
                dst = ss.get(st.split(":", 1)[1], {})
                assert dst.get("moved_from") == d and dst.get("session_type") != "rest", (d, dst)


def test_m2_a_day_the_budget_rested_is_not_a_free_slot():
    import app as app_module
    plan, tue = _missed_week_plan()
    for s in plan["weeks"][0]["sessions"]:
        if s["day"] == "2026-09-19":
            s["description"] = tp.BUDGET_REST
            s["rest_reason"] = "budget"
    clock.freeze(G0 + _dt.timedelta(days=3))
    assert not [x for x in app_module._compute_missed_suggestions(plan, G0 + _dt.timedelta(days=3))
                if x.get("suggested_date") == "2026-09-19"]


# M3 (evaluation of b7be0395): the final step never serves content above the
# slot's class -- a downshift stays downshifted (D3/D4).

_RANK = {"recovery": 0, "endurance": 0, "endurance_intervals": 0,
         "tempo": 1, "tempo_intervals": 1, "tempo_ladder": 1,
         "sweet_spot": 2, "sweet_spot_ladder": 2,
         "threshold": 3, "threshold_ladder": 3, "over_under": 3,
         "vo2max": 4, "vo2_short": 4, "vo2_ladder": 4,
         "anaerobic": 4, "neuromuscular": 4}
_SLOT = {"recovery": 0, "z2": 0, "long_z2": 0, "tempo": 1, "sweetspot": 2,
         "threshold": 3, "overunder": 3, "vo2max": 4, "sprint": 4}


@pytest.mark.parametrize("slot", ["tempo", "sweetspot", "threshold", "z2"])
@pytest.mark.parametrize("dur", [45, 60, 83, 110])
def test_m3_a_fileless_slot_is_served_content_no_harder_than_it(slot, dur):
    """The G3 downshift turned the owner's 10-01 sweetspot into tempo and
    emptied its file; the final step served sweetspot_3x750s-210s_92pct_83min
    (sweet-spot content, IF 0.81) where base served tempo_1x70min_78pct."""
    rows = {r["File"]: r for r in tp.load_workout_library()}
    t = _MON
    s = _s(3, slot, dur, round(dur / 60 * tp.TSS_PER_HOUR[slot]), "")
    wk = _w([s], target=900)
    clock.freeze(t)
    tp.finalize_weeks([wk], _goal(daily_max_hours={d: 3.0 for d in range(6)}))
    got = wk.sessions[0]
    cc = tp._content_class_for_row(rows[got.zwo_file])
    assert _RANK.get(cc, 0) <= _SLOT[slot], (slot, dur, got.zwo_file, cc)


def test_m3_a_file_above_its_slot_is_served_again():
    """A tempo slot still holding the sweet-spot file it was downshifted from
    (a pass that re-typed it without re-matching) rides as sweet spot."""
    rows = {r["File"]: r for r in tp.load_workout_library()}
    ss = "tempo_5x1min-1min_115pct_60min.zwo"                # classified sweet_spot
    assert tp._content_class_for_row(rows[ss]) == "sweet_spot"
    wk = _w([_s(3, "tempo", 60, 65, ss)], target=900)
    clock.freeze(_MON)
    tp.finalize_weeks([wk], _goal())
    got = wk.sessions[0].zwo_file
    assert _RANK[tp._content_class_for_row(rows[got])] <= 1, got


def test_m3_the_owner_downshift_stays_tempo():
    plan = {"goal": tp.goal_to_dict(owner()), "weeks": [tp.week_to_dict(w) for w in gen(owner())]}
    day = next(s for w in plan["weeks"] for s in w["sessions"]
               if s["day"] > "2026-09-20" and s["session_type"] == "sweetspot")
    day.update(session_type="tempo", zwo_file="", zwo_name="", adapted=True,
               description="G3 polarization breach: sweetspot → tempo (Seiler/Stöggl/Treff)")
    clock.freeze(G0 + _dt.timedelta(days=3))
    tp.reforecast_dict(plan, tsb_series={})
    got = next(s for w in plan["weeks"] for s in w["sessions"] if s["day"] == day["day"])
    rows = {r["File"]: r for r in tp.load_workout_library()}
    assert got["session_type"] == "tempo"
    assert _RANK[tp._content_class_for_row(rows[got["zwo_file"]])] <= 1, got["zwo_file"]


# D-D / D-C (evaluation of b7be0395): the last shortening leaves every slot
# a file that fits it, and the unload week is lightest after the final step.

def _too_long(weeks, t):
    """Pending sessions whose file runs past the slot's band when the library
    holds a file of the same class inside it (the coherence pass's own test)."""
    lib = tp.load_workout_library()
    rows = {r["File"]: r for r in lib}
    out = []
    for w in weeks:
        for s in w.sessions:
            if (s.day < t or s.session_type in ("rest", "ftp_test") or s.is_race
                    or getattr(s, "is_opener", False) or s.status != "pending"):
                continue
            r = rows.get(s.zwo_file)
            if r is None:
                continue
            fd, band = float(r["Duration(min)"]), tp._slot_file_band_min(s.duration_min)
            if fd - s.duration_min <= band:
                continue
            cc = tp._content_class_for_row(r)
            if any(tp._content_class_for_row(x) == cc
                   and abs(float(x["Duration(min)"] or 0) - s.duration_min) <= band for x in lib):
                out.append((str(s.day), s.session_type, s.duration_min, s.zwo_file))
    return out


def _sweep_goal(kind, t, rest, daily, hpw, weeks=0, race_in=None):
    """The reviewer's sweep riders (zz_probe/sweep_gen.py), as goals."""
    td = None
    if race_in is not None:
        d = t + _dt.timedelta(weeks=race_in)
        td = d + _dt.timedelta(days=(6 - d.weekday()) % 7)
        weeks = max(4, -(-(td - t).days // 7)) if kind == "event" else weeks
    return tp.Goal(goal_type=kind, target_date=td, rest_days=list(rest),
                   available_days=[d for d in range(7) if d not in rest],
                   daily_max_hours=dict(daily), max_weekday_hours=2.0, max_weekend_hours=3.5,
                   hours_per_week=hpw, plan_weeks=weeks,
                   event_km=(160 if kind == "event" else 0),
                   event_climb_m=(2000 if kind == "event" else 0))


@pytest.mark.parametrize("case", [
    # rider, day: where 02a0f514..fe6b0b36's last shortening left the slot
    # shorter than its file (z2 33 min serving a 60 min file; 58 vs 75; 72 vs 90)
    ("everyday-vo2", "2026-09-17"), ("owner-event12", "2026-10-06"),
    ("event6-defaults", "2026-09-18")])
def test_dd_no_slot_is_left_shorter_than_its_file(case, monkeypatch):
    import config
    monkeypatch.setattr(config, "ICU_ATHLETE_ID", "gate-athlete")    # the sweep's seed
    name, day = case
    t = _dt.date.fromisoformat(day)
    g, ctl, rwt, ftp = {
        "everyday-vo2": (lambda: _sweep_goal("vo2max", t, [], {d: 1.0 for d in range(7)}, 7,
                                             weeks=8), 45, 300, 300),
        "owner-event12": (lambda: _sweep_goal("event", t, [4], {**DAILY, 5: 3.0}, 12.5,
                                              race_in=12), 55, 380, 247),
        "event6-defaults": (lambda: _sweep_goal("event", t, [0], {}, 10, race_in=6),
                            50, 380, 247)}[name]
    clock.freeze(t)
    weeks = tp.generate_plan(g(), seed_salt=t.toordinal(), current_ctl=ctl,
                             recent_weekly_tss=rwt, athlete={"ftp": ftp, "weight_kg": 72})[1]
    assert not _too_long(weeks, t)


def test_dc_the_unload_week_is_lightest_after_the_final_step(monkeypatch):
    """weekend-event8 generated 2026-10-06: the unload week came out at 80 TSS
    against 78 in its block, because the unload pass ran before the last
    re-match and re-stamp (D-C)."""
    import config
    monkeypatch.setattr(config, "ICU_ATHLETE_ID", "gate-athlete")    # the sweep's seed
    t = _dt.date(2026, 10, 6)
    g = lambda: _sweep_goal("event", t, [0, 1, 2, 3, 4], {5: 4.0, 6: 4.0}, 8, race_in=8)  # noqa: E731
    clock.freeze(t)
    weeks = tp.generate_plan(g(), seed_salt=t.toordinal(), current_ctl=40,
                             recent_weekly_tss=250, athlete={"ftp": 247, "weight_kg": 72})[1]
    assert not [str(v) for v in pi.audit(weeks, g(), today=t) if v.rule == "stepback_lightest"]


def test_dc_an_extended_block_keeps_its_unload_week_lightest(monkeypatch):
    """The reviewer's sweep, owner-cont generated Thursday 2026-09-17 and
    extended on the Sunday: the unload week came out at 322 TSS against 281 in
    its block -- the unload pass ran in generate alone (D-C)."""
    import config
    monkeypatch.setattr(config, "ICU_ATHLETE_ID", "gate-athlete")
    b0, t = _dt.date(2026, 9, 17), _dt.date(2026, 9, 20)
    g = lambda: _sweep_goal("continuous", b0, [4], {**DAILY, 5: 3.0}, 12.5)  # noqa: E731
    clock.freeze(b0)
    base = tp.generate_plan(g(), seed_salt=b0.toordinal(), current_ctl=55,
                            recent_weekly_tss=380, athlete={"ftp": 247})[1]
    rides = [{"date": (b0 + _dt.timedelta(days=i)).isoformat(), "tss": 60.0}
             for i in range(3) if (b0 + _dt.timedelta(days=i)).weekday() != 4]
    clock.freeze(t)
    weeks = tp.extend_continuous_plan(g(), copy.deepcopy(base), 55, recent_activities=record(rides),
                                      athlete={"ftp": 247}, recent_weekly_tss=380,
                                      seed_salt=7)[1]
    assert not [str(v) for v in pi.audit(weeks, g(), today=t) if v.rule == "stepback_lightest"]


def test_dd_the_re_match_does_not_take_the_week_back_over_its_budget(monkeypatch):
    """weekend-event8 generated Sunday 2026-09-27: one ordered round left the
    week at 56 TSS against a 46 budget -- the re-match to the band served a
    heavier file after the budget was set, so the final step repeats budget,
    unload and re-match until they agree."""
    import config
    monkeypatch.setattr(config, "ICU_ATHLETE_ID", "gate-athlete")
    t = _dt.date(2026, 9, 27)
    g = lambda: _sweep_goal("event", t, [0, 1, 2, 3, 4], {5: 4.0, 6: 4.0}, 8, race_in=8)  # noqa: E731
    clock.freeze(t)
    weeks = tp.generate_plan(g(), seed_salt=t.toordinal(), current_ctl=40,
                             recent_weekly_tss=250, athlete={"ftp": 247, "weight_kg": 72})[1]
    assert not [str(v) for v in pi.audit(weeks, g(), today=t) if v.rule == "weekly_volume"]


# MF-1 (second evaluation, afec7c6a): a missed session the auto-reschedule
# moved is never destroyed by ANY later entry point -- the guarantee lives in
# the final step's record step, reading the plan's auto_moves records.

_TUE, _SAT = "2026-09-15", "2026-09-19"
_LIGHT = [{"id": "r1", "date": "2026-09-14", "sport": "Ride", "tss": 60.0}]


def _moved_plan():
    """Sync 1 has moved Tuesday's missed z2 to a free Saturday."""
    import app as app_module
    plan, tue = _missed_week_plan()
    for s in plan["weeks"][0]["sessions"]:
        if s["day"] == "2026-09-14":
            s["status"] = "done"
    t = G0 + _dt.timedelta(days=3)
    clock.freeze(t)
    plan = app_module._apply_plan_update(plan, training={}, activities=record(_LIGHT), today=t)[0]
    assert [s for w in plan["weeks"] for s in w["sessions"]
            if s["day"] == _SAT and s.get("moved_from") == _TUE], "sync 1 did not move it"
    return plan


def _nowhere(plan):
    ss = {s["day"]: s for w in plan["weeks"] for s in w["sessions"]}
    tue = ss[_TUE]
    held = [s for s in ss.values() if s.get("moved_from") == _TUE and s["session_type"] != "rest"]
    return not (tue.get("status") == "missed" and tue["session_type"] != "rest") and not held


def test_mf1_a_later_sync_that_rests_the_destination_restores_the_session(ride_record):
    import app as app_module
    plan = _moved_plan()
    t = G0 + _dt.timedelta(days=3)
    clock.freeze(t)
    big = _LIGHT + [{"id": "r2", "date": "2026-09-16", "sport": "Ride", "tss": 600.0}]
    ride_record(big)
    plan = app_module._apply_plan_update(plan, training={}, activities=big, today=t)[0]
    assert not _nowhere(plan)
    tue = next(s for w in plan["weeks"] for s in w["sessions"] if s["day"] == _TUE)
    assert tue["status"] == "missed" and tue["session_type"] == "z2"


def _through_app(tmp_path, monkeypatch, plan, url, body):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    import app as app_module
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    (tmp_path / "current_plan.json").write_text(json.dumps(plan))
    import ride_storage
    monkeypatch.setattr(ride_storage, "ride_record", lambda: list(_LIGHT))   # the one seam
    clock.freeze(G0 + _dt.timedelta(days=3))
    with patch("db.query_activities", return_value=_LIGHT):
        r = TestClient(app_module.app).post(url, json=body)
    assert r.status_code == 200, r.text
    return json.loads((tmp_path / "current_plan.json").read_text())


def test_mf1_zeroing_the_destination_restores_the_session(tmp_path, monkeypatch):
    plan = _through_app(tmp_path, monkeypatch, _moved_plan(), "/api/plan/save-availability",
                        {"availability": {_SAT: {"hours": 0, "type": "unavailable"}}})
    assert not _nowhere(plan)


def test_mf1_a_regenerate_after_a_move_restores_the_session(tmp_path, monkeypatch):
    plan = _through_app(tmp_path, monkeypatch, _moved_plan(), "/api/plan/regenerate", {})
    assert not _nowhere(plan)


# MF-2 (second evaluation): a rest a pass wrote for a reason is marked with
# a reason code where it is written, and the auto-move reads the code.

@pytest.mark.parametrize("writer", ["plan_week spent budget", "ceiling", "unload week"])
def test_mf2_every_reasoned_rest_carries_its_reason(writer):
    s = _s(3, "z2", 60, 45)
    wk = _w([s], target=900)
    if writer == "plan_week spent budget":
        clock.freeze(_MON)
        pw = tp.plan_week(1, _MON, tp.Phase(name="base", start=_MON, end=_MON + _dt.timedelta(days=6),
                                             weeks=1, focus="", weekly_tss_target=100, z2_pct=80,
                                             hit_per_week=1, session_types=["z2"]),
                          _goal(), False, completed_tss=400, tss_target=100)
        rests = [x for x in pw.sessions if x.session_type == "rest" and x.day.weekday() != 6]
        assert rests and all(getattr(x, "rest_reason", "") == "budget" for x in rests), \
            [(x.day, x.description, getattr(x, "rest_reason", None)) for x in rests]
        return
    if writer == "ceiling":
        wk = _w([_s(0, "z2", 120, 90), _s(1, "z2", 120, 90), _s(2, "z2", 120, 90)], target=90)
        tp._enforce_weekly_volume_ceiling([wk])
        rests = [x for x in wk.sessions if x.session_type == "rest"]
        assert rests and all(x.rest_reason == "budget" for x in rests)
        return
    if writer == "unload week":
        b = [_w([_s(7 * i + d, "z2", 60, 45) for d in range(6)] + [_s(7 * i + 6, "rest", 0, 0, "")],
                n=i + 1, first=7 * i, last=7 * i + 6) for i in range(3)]
        sb = _w([_s(21 + d, "z2", 90, 70) for d in range(6)] + [_s(27, "rest", 0, 0, "")],
                n=4, first=21, last=27, stepback=True, target=100)
        clock.freeze(_MON)
        tp._enforce_stepback_is_lightest(b + [sb])
        rests = [x for x in sb.sessions if x.session_type == "rest" and x.day != _d(27)]
        assert rests and all(x.rest_reason == "unload" for x in rests)


@pytest.mark.parametrize("reason", ["budget", "unload", "race_week", "unavailable", "readiness"])
def test_mf2_a_reasoned_rest_is_not_a_free_slot(reason):
    """Whatever its text says -- the evaluator's test_B used plan_week's
    dynamic "Rest — this week's TSS budget is already spent (300 of 400)."
    and the week owner's "Rest -- no room left in the week"."""
    import app as app_module
    plan, tue = _missed_week_plan()
    for s in plan["weeks"][0]["sessions"]:
        if s["day"] in ("2026-09-19", "2026-09-20"):
            s.update(session_type="rest", duration_min=0, tss_estimate=0, zwo_file="",
                     description="Rest -- no room left in the week", rest_reason=reason)
    t = G0 + _dt.timedelta(days=3)
    assert not [x for x in app_module._compute_missed_suggestions(plan, t)
                if x.get("suggested_date") in ("2026-09-19", "2026-09-20")]


# L-1 / L-2 (second evaluation): one owner for ridden load, and it survives
# the shapes real archives hold.

def _R(i, start, secs, t, sport="Ride", **kw):
    d = {"id": i, "date": start[:10], "sport": sport, "tss": float(t),
         "raw_json": json.dumps({"start_date_local": start, "elapsed_time": secs})}
    d.update(kw)
    return d


def _band(i, start, secs, t, sport="VirtualRide"):
    """The Garmin band's recording: HR load, no power (the owner's rows)."""
    return {"id": i, "date": start[:10], "sport": sport, "tss": float(t), "avg_power": None,
            "raw_json": json.dumps({"start_date_local": start, "elapsed_time": secs,
                                    "power_load": None, "hr_load": t})}


def _bryton(i, start, secs, t):
    """A Bryton upload as the owner's rows carry it: avg_power NULL on the
    row, the power in raw_json (power_load)."""
    return {"id": i, "date": start[:10], "sport": "Ride", "tss": float(t), "avg_power": None,
            "raw_json": json.dumps({"start_date_local": start, "elapsed_time": secs,
                                    "power_load": t})}


@pytest.mark.parametrize("name, acts, want", [
    ("morning and evening both count", [_R("a", "2026-09-21T07:00:00", 3600, 40),
                                        _R("b", "2026-09-21T18:00:00", 3600, 60)], 100),
    ("a duplicate without elapsed time", [
        {"id": "a", "date": "2026-09-21", "sport": "Ride", "tss": 50.0,
         "raw_json": json.dumps({"start_date_local": "2026-09-21T10:00:00"})},
        {"id": "b", "date": "2026-09-21", "sport": "VirtualRide", "tss": 45.0,
         "raw_json": json.dumps({"start_date_local": "2026-09-21T10:00:30"})}], (45, 50)),
    ("a local-time row and a UTC started_at row of one ride", [
        _R("a", "2026-09-21T12:00:00", 3600, 60),
        {"id": "b", "date": "2026-09-21", "sport": "Ride", "tss": 58.0,
         "started_at": "2026-09-21T10:00:30Z", "duration_sec": 3550}], (58, 60)),
    ("a band recording bridging two real rides", [
        _R("a", "2026-09-21T08:00:00", 7200, 60), _band("b", "2026-09-21T09:55:00", 30600, 90),
        _R("c", "2026-09-21T17:00:00", 7200, 60)], 120),
    ("power read where real rows carry it (the owner's Wednesday)", [
        _bryton("i189542797", "2026-09-23T11:30:19", 5931, 18),
        _band("i189708913", "2026-09-23T11:30:50", 6572, 13)], 18),
    ("the power meter's recording wins even when the band's is shorter", [
        _bryton("p", "2026-09-23T11:30:00", 7000, 18),
        _band("h", "2026-09-23T11:31:00", 6000, 13)], 18),
    ("back to back rides both count", [_R("a", "2026-09-21T10:00:00", 3600, 50),
                                       _R("b", "2026-09-21T11:00:00", 3600, 50)], 100),
    ("a long stop, then a second ride", [_R("a", "2026-09-21T09:00:00", 4 * 3600, 50),
                                         _R("b", "2026-09-21T12:30:00", 3600, 50)], 100),
    ("a run is not a ride", [_R("a", "2026-09-21T10:00:00", 3600, 50, "Run")], 0),
])
def test_l2_rides_once_edge_cases(name, acts, want, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Paris")
    import time
    time.tzset()
    try:
        got = tp._completed_tss_in(acts, _dt.date(2026, 9, 21), _dt.date(2026, 9, 23))
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert got in (want if isinstance(want, tuple) else (want,)), (name, got)


def test_l1_every_ridden_load_reader_counts_rides_once():
    """The owner's chronic load read their walk and the band's VirtualRide
    duplicate: ride_storage.chronic_weekly_tss and the auditor's
    plan_invariants._ride_tss summed every activity."""
    import ride_storage
    t = _dt.date(2026, 11, 30)
    clean, noisy = [], []
    d = t - _dt.timedelta(days=70)
    k = 0
    while d < t:
        k += 1
        ride = {"ride_id": f"icu_r{k}", "external_id": f"r{k}", "sport": "Ride",
                "started_at": f"{d.isoformat()}T12:00:00", "duration_s": 7200,
                "avg_power_w": 180, "tss": 80.0}
        clean.append(ride)
        noisy += [ride,
                  {"ride_id": f"icu_b{k}", "external_id": f"b{k}", "sport": "VirtualRide",
                   "started_at": f"{d.isoformat()}T12:01:00", "duration_s": 7300,
                   "avg_power_w": None, "tss": 70.0},
                  {"ride_id": f"icu_w{k}", "external_id": f"w{k}", "sport": "Walk",
                   "started_at": f"{d.isoformat()}T18:00:00", "duration_s": 3600, "tss": 6.0}]
        d += _dt.timedelta(days=2)
    assert ride_storage.chronic_weekly_tss(noisy, today=t) == \
        ride_storage.chronic_weekly_tss(clean, today=t)
    lo, hi = t - _dt.timedelta(days=7), t - _dt.timedelta(days=1)
    as_dates = lambda rs: [{**r, "date": r["started_at"][:10]} for r in rs]  # noqa: E731
    assert pi._ride_tss(as_dates(noisy), lo, hi) == pi._ride_tss(as_dates(clean), lo, hi)


# L-3 (second evaluation): regenerate is idempotent. A session regenerate
# keeps (adapted, user-moved, ...) came in with its stored TSS; the final step
# re-stamped it to its served load (6.8) AFTER the load ramp had read it, so
# the second regenerate read a different week and moved the weeks after it.

def test_l3_regenerate_twice_gives_the_same_plan(ride_record):
    base = gen(owner("continuous"))
    t = G0 + _dt.timedelta(days=3)
    wk2 = next(w for w in base if w.start > t)
    for s in wk2.sessions:
        if s.session_type != "rest" and s.zwo_file:
            s.adapted = True
            s.tss_estimate = round(float(s.tss_estimate) * 0.7)   # a stored, stale load

    rides = ride_days(t - _dt.timedelta(days=14), t - _dt.timedelta(days=1))
    ride_record(rides)                                   # the one seam

    def regen(weeks):
        clock.freeze(t)
        g = owner("continuous", plan_weeks=0)
        g.target_date = t + _dt.timedelta(weeks=12)
        return tp.regenerate_from_today(
            g, copy.deepcopy(weeks), 55, athlete=ATH, recent_weekly_tss=380,
            activities=rides)[1]
    once = regen(base)
    twice = regen(once)
    fp = lambda ws: [(w.start, w.tss_target, [(s.day, s.session_type, s.duration_min,  # noqa: E731
                                              s.tss_estimate, s.zwo_file) for s in w.sessions])
                     for w in ws]
    assert fp(once) == fp(twice)


# L-4 (second evaluation): a served file fits its slot, or the slot takes the
# file's length when the day and the week have room for it.

def _lib_of(*files):
    rows = {r["File"]: r for r in tp.load_workout_library()}
    return [rows[f] for f in files]


_ANA59 = "anaerobic_2x2min-2min_125pct_59min.zwo"


def test_l4_a_file_that_fits_is_served_first():
    """A slot the anaerobic ceiling cut to 50 gets an anaerobic file that
    fits 50 minutes when the library has one."""
    wk = _w([_s(1, "vo2max", 59, 70, _ANA59), _s(3, "z2", 60, 45)], target=900)
    clock.freeze(_MON)
    tp.finalize_weeks([wk], _goal())
    s = wk.sessions[0]
    rows = {r["File"]: r for r in tp.load_workout_library()}
    assert s.duration_min == 50 and float(rows[s.zwo_file]["Duration(min)"]) <= 50, \
        (s.duration_min, s.zwo_file)


def test_l4_a_slot_takes_its_files_length_when_the_day_and_week_allow():
    """No file fits (a library of one): a 70-min endurance slot serving a
    90-min file takes the file's 90 minutes rather than leaving 20 the week
    never counted."""
    lib = _lib_of(Z2)                                   # z2_steady_68pct_90min
    wk = _w([_s(1, "z2", 70, 50, Z2), _s(3, "z2", 60, 45, Z2)], target=900)
    clock.freeze(_MON)
    tp.finalize_weeks([wk], _goal(), library=lib)
    assert wk.sessions[0].duration_min == 90


def test_l4_a_hard_slot_never_grows_past_its_types_ceiling():
    """The anaerobic ceiling is 50 min (FIX-2): a 59-min anaerobic file with
    nothing that fits keeps its 50-min slot."""
    lib = _lib_of(_ANA59, Z2)
    wk = _w([_s(1, "vo2max", 59, 70, _ANA59), _s(3, "z2", 60, 45)], target=900)
    clock.freeze(_MON)
    tp.finalize_weeks([wk], _goal(), library=lib)
    assert wk.sessions[0].duration_min == 50


def test_l4_the_slot_does_not_grow_past_the_day_or_the_week():
    lib = _lib_of(Z2)
    tight_day = _goal(daily_max_hours={d: (75 / 60 if d == 1 else 2.0) for d in range(6)})
    wk = _w([_s(1, "z2", 70, 50, Z2)], target=900)
    clock.freeze(_MON)
    tp.finalize_weeks([wk], tight_day, library=lib)
    assert wk.sessions[0].duration_min <= 75
    wk = _w([_s(1, "z2", 70, 50, Z2), _s(3, "z2", 60, 45, Z2)], target=100)
    tp.finalize_weeks([wk], _goal(), library=lib)
    assert wk.sessions[0].duration_min < 90
    assert sum(s.tss_estimate for s in wk.sessions) <= 100 + 1
    assert sum(s.tss_estimate for s in wk.sessions) <= 100 + 1


def test_l3_l4_an_adapted_kept_session_is_not_refitted(monkeypatch):
    """d05724d2's refit re-served the owner's adapted 09-24 (a 95-min file on
    a 90-min slot, TSS 68 -> 54) after the ramp had read it: two regenerates
    disagreed again (week 09-28's target 381 -> 373). And a TSB-eased day that
    was refitted no longer matched what the easing wrote, so it was never
    restored (test_reforecast_fatigue)."""
    base = gen(owner("continuous"))
    t = G0 + _dt.timedelta(days=3)
    rows = {r["File"]: r for r in tp.load_workout_library()}
    longer = next(f for f, r in rows.items() if f.startswith("endurance_steady_68pct_95min"))
    wk2 = next(w for w in base if w.start > t)
    s = next(x for x in wk2.sessions if x.session_type == "z2")
    s.adapted, s.zwo_file, s.duration_min, s.tss_estimate = True, longer, 80, 60
    clock.freeze(t)
    tp.finalize_weeks(base, owner("continuous"))
    assert s.zwo_file == longer and s.duration_min == 80
