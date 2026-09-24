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
