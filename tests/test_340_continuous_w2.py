"""3.4.0 W2 (IP_CONTINUOUS_MODE amendments C+D) — deload triggers + rotation.

C — the already-computed monotony/ACWR series advance the deload in
    continuous plans: monotony >= 2.0 or ACWR > 1.5 (grill P4) converts the
    CURRENT week to the deload shape via plan_week(is_stepback=True) +
    match_zwo, never two deloads back-to-back, revertible + week-latched
    (DFA auto-swap pattern).
D — continuous_policy.today_headline (slim/9.3 H1-H3, which replaced the
    rotation policy suggest_today_family): the session the rider is told to
    ride, by its goal zone, with the HRV-gated policy's gates (LnRMSSD7d vs
    SWC two-sided, TSB floor, 48h spacing + glyco day-after, deload week) as
    cautions, exposed as /api/today-session `continuous_suggestion` —
    additive, absent for finite goals.

Hermetic: policy fns are pure; app-level tests pin `today`, patch
metrics/rides/plan-dir, never touch the network (conftest gates), and the
tracked workouts/.library_index.json is snapshotted + restored around the
module (the planner rewrites it on load).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
import continuous_policy as cpol
import training_planner as tp

_LIB_INDEX = Path(__file__).resolve().parent.parent / "src" / "workouts" / ".library_index.json"

HARD_TYPES = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}

GREEN = {"ln_rmssd_7d": 3.90, "swc_lower": 3.70, "swc_upper": 4.10, "tsb": 5.0}


@pytest.fixture(scope="module", autouse=True)
def _restore_library_index():
    backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None
    yield
    if backup is not None:
        _LIB_INDEX.write_bytes(backup)


# ═══════════════════════════════════════════════════════════════════════════
# D — today's headline (pure, no I/O)
# ═══════════════════════════════════════════════════════════════════════════

def _head(st="vo2max", goal=3, readiness=None, dsa=3, deload=False, owed=None, **kw):
    return cpol.today_headline(
        session_type=st, goal_zone=goal,
        readiness=readiness if readiness is not None else dict(GREEN),
        days_since_last_anaerobic=dsa, deload_week=deload, owed=owed, **kw)


def _assert_shape(out):
    assert set(out) == {"family", "goal_zone", "session_type", "reason"}
    assert out["family"] in cpol.FAMILIES
    assert isinstance(out["reason"], str) and out["reason"].endswith(".")


@pytest.mark.parametrize("st,goal,family", [
    ("recovery", 1, "zone_1"), ("z2", 1, "zone_1"), ("threshold", 2, "zone_2"),
    ("overunder", 2, "zone_2"), ("vo2max", 3, "zone_3"), ("sprint", 3, "zone_3")])
def test_headline_names_the_session_and_its_zone(st, goal, family):
    out = _head(st, goal)
    _assert_shape(out)
    assert (out["family"], out["goal_zone"], out["session_type"]) == (family, goal, st)
    assert out["reason"].startswith(f"{cpol.SESSION_LABEL[st]} (zone {goal}) today, as planned.")
    assert "HRV in band." in out["reason"]
    # VO2max is aerobic work in zone 3, never "anaerobic" (owner's D1)
    assert "naerobic" not in out["reason"]


def test_the_owners_recovery_monday():
    """The reported case: a planned recovery Monday with no HRV and the
    week's hard minutes on Tue/Thu (held by those days) reads as recovery."""
    out = _head("recovery", 1, readiness={"tsb": 5.0}, dsa=None,
                owed={"zone1": 0, "zone2": 0, "zone3": 0})
    assert out["family"] == "zone_1"
    assert out["reason"] == "Recovery (zone 1) today, as planned. No HRV baseline yet."


def test_rest_day_says_rest():
    out = _head("rest", None)
    _assert_shape(out)
    assert (out["family"], out["goal_zone"]) == ("rest", None)
    assert out["reason"].startswith("Rest today, as planned.")


def test_adjusted_session_is_the_headline():
    out = _head("z2", 1, planned_type="vo2max",
                adjustment_reason="HRV below SWC day 1 → capped at Z2 (was vo2max)")
    assert out["family"] == "zone_1" and out["session_type"] == "z2"
    assert out["reason"].startswith("Endurance (zone 1) today, adjusted from VO2max: HRV below SWC")


@pytest.mark.parametrize("readiness,dsa,deload,needle", [
    (dict(GREEN, ln_rmssd_7d=3.50), 3, False, "below your normal band"),
    (dict(GREEN, ln_rmssd_7d=4.30), 3, False, "above your normal band"),
    (dict(GREEN, tsb=-31.0), 3, False, "TSB -31"),
    (dict(GREEN), 1, False, "48h"),
    (dict(GREEN), 3, True, "deload week"),
])
def test_gates_are_cautions_on_a_hard_day_never_another_family(readiness, dsa, deload, needle):
    out = _head("vo2max", 3, readiness=readiness, dsa=dsa, deload=deload)
    _assert_shape(out)
    assert out["family"] == "zone_3"          # the session, not "low aerobic"
    assert "Caution:" in out["reason"] and needle in out["reason"]
    # on an easy day the same gate is not a caution
    easy = _head("z2", 1, readiness=readiness, dsa=dsa, deload=deload)
    assert easy["family"] == "zone_1" and "Caution:" not in easy["reason"]


def test_tsb_floor_is_strict():
    assert "Caution" not in _head(readiness=dict(GREEN, tsb=-30.0))["reason"]


def test_the_deep_fatigue_floor_is_reforecasts():
    """The headline's floor mirrors reforecast's fatigue easing: the policy
    module keeps no repo imports, so a test keeps the two equal."""
    import continuous_policy
    import training_planner
    assert continuous_policy.TSB_LOW_FLOOR == training_planner.TSB_EASE_BELOW


def test_owed_minutes_cite_the_zone_above_the_floor():
    out = _head("z2", 1, owed={"zone2": 40, "zone3": 12})
    assert "Zone 2 still owed 40min" in out["reason"]
    assert "Zone 3 still owed 12min" in out["reason"]
    # sub-floor gaps are zone-edge noise (zone 2: 10 min, zone 3: 2 min)
    out = _head("z2", 1, owed={"zone2": 9, "zone3": 1})
    assert "owed" not in out["reason"]


def test_empty_inputs_never_crash():
    out = cpol.today_headline("z2", 1, None, None)
    _assert_shape(out)
    assert out["family"] == "zone_1"


# ═══════════════════════════════════════════════════════════════════════════
# C — trigger fns (pure)
# ═══════════════════════════════════════════════════════════════════════════

def test_deload_trigger_thresholds():
    assert cpol.deload_trigger(None, None) is None
    assert cpol.deload_trigger(1.99, 1.50) is None       # both under the line
    trip = cpol.deload_trigger(2.0, None)                # monotony >= 2.0
    assert trip["trigger"] == "monotony" and trip["value"] == 2.0
    trip = cpol.deload_trigger(None, 1.51)               # ACWR strictly > 1.5
    assert trip["trigger"] == "acwr" and trip["value"] == 1.51
    trip = cpol.deload_trigger(2.4, 1.9)                 # monotony first (P4)
    assert trip["trigger"] == "monotony"
    assert trip["reason"].endswith(".")


def test_foster_monotony_math():
    assert cpol.foster_monotony([]) is None
    assert cpol.foster_monotony([0] * 7) is None
    assert cpol.foster_monotony([70] * 7) == cpol.MONOTONY_CAP  # SD=0, capped
    daily = [100, 0, 80, 0, 90, 0, 60]
    m = cpol.foster_monotony(daily)
    mean = sum(daily) / 7
    sd = (sum((v - mean) ** 2 for v in daily) / 7) ** 0.5
    assert m == pytest.approx(mean / sd)
    assert m < 2.0  # a properly varied week never trips


# ═══════════════════════════════════════════════════════════════════════════
# C — deload advance on the stored plan (app-level, pinned today)
# ═══════════════════════════════════════════════════════════════════════════

def _mk_session(d: date, session_type: str, dur: int, tss: int) -> dict:
    return {"day": d.isoformat(), "day_name": d.strftime("%a"),
            "session_type": session_type, "duration_min": dur,
            "tss_estimate": tss, "description": f"{session_type} {dur}min",
            "zwo_file": "", "zwo_name": "", "status": "pending"}


_WEEK_SHAPE = [("rest", 0, 0), ("vo2max", 60, 75), ("z2", 60, 45),
               ("threshold", 60, 70), ("rest", 0, 0), ("long_z2", 120, 90),
               ("recovery", 45, 25)]


def _mk_week(week_num: int, monday: date, is_stepback=False,
             tss_target=400) -> dict:
    sessions = [_mk_session(monday + timedelta(days=i), st, dur, tss)
                for i, (st, dur, tss) in enumerate(_WEEK_SHAPE)]
    return {"week_num": week_num, "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "phase": "continuous", "tss_target": tss_target,
            "is_stepback": is_stepback, "sessions": sessions}


def _mk_continuous_plan(today: date, flags=(False, False, False, True),
                        first_week_num=1, weeks_back=0) -> dict:
    """Continuous plan whose week[weeks_back] contains `today`."""
    monday = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks_back)
    return {
        "goal": {"type": "continuous", "focus": "both", "hours_per_week": 8.0,
                 "rest_days": [0, 4], "available_days": [1, 2, 3, 5, 6],
                 "distribution": "polarized"},
        "phases": [{"name": "continuous", "weeks": len(flags),
                    "start": monday.isoformat(),
                    "end": (monday + timedelta(weeks=len(flags), days=-1)).isoformat(),
                    "weekly_tss": 400, "focus": "rolling"}],
        "weeks": [_mk_week(first_week_num + i, monday + timedelta(weeks=i),
                           is_stepback=f) for i, f in enumerate(flags)],
        "generated": "2026-06-01T00:00:00",
    }


def _monotone_rides(today: date, tss=70, n=7) -> list[dict]:
    """n identical daily loads ending today → Foster monotony = cap (>= 2)."""
    return [{"started_at": f"{(today - timedelta(days=i)).isoformat()}T10:00:00",
             "tss": tss, "time_in_zone": {"z1": 600, "z2": 2400}}
            for i in range(n)]


def _calm_rides(today: date) -> list[dict]:
    """Varied light week → monotony well under 2.0, no ACWR signal."""
    return [{"started_at": f"{(today - timedelta(days=i)).isoformat()}T10:00:00",
             "tss": t, "time_in_zone": {"z1": 300, "z2": 1800}}
            for i, t in [(1, 90), (3, 30), (5, 60)]]


class DeloadAdvanceBase(unittest.TestCase):
    def setUp(self):
        app_module.clear_cache()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp
        self.today = date.today()

    def tearDown(self):
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _write(self, plan: dict) -> Path:
        p = self._tmp / "current_plan.json"
        p.write_text(json.dumps(plan))
        return p

    def _advance(self, plan, rides):
        """As its caller (_maybe_auto_reforecast) runs it since contract 4.1:
        inside the plan store's read-modify-write, which persists what the
        helper put in the plan it was handed."""
        path = self._write(plan)
        out = {}

        def _m(p):
            before = p.get("deload_advance")
            out["chip"] = app_module._maybe_advance_continuous_deload(
                p, path, today=self.today)
            return p if p.get("deload_advance") is not before else None

        with patch.object(app_module, "_load_all_rides_safe", return_value=rides):
            tp.update_plan(path, _m)
        plan.clear()
        plan.update(json.loads(path.read_text()))
        return out["chip"], path


class TestTodayCardShowsTheFreshDeload(DeloadAdvanceBase):
    """/api/today-session reads today's session through the week view. After
    the sync advances the deload, the card must show the refit session the
    advance wrote, with its chip (the adversarial review of 2026-09-14 found
    no test caught a view built from the plan before the advance)."""

    def test_the_refit_session_is_what_the_card_says(self):
        import clock
        tue = date(2026, 9, 15)
        clock.freeze(tue)
        self.addCleanup(clock.unfreeze)
        self._write(_mk_continuous_plan(tue))
        with patch.object(app_module, "_load_all_rides_safe", return_value=_monotone_rides(tue)), \
             patch.object(app_module, "_kick_lazy_icu_sync", return_value=None), \
             patch.object(app_module, "_sync_icu_activities",
                          return_value={"added": 0, "updated": 0, "status": "no_credentials"}), \
             patch.object(app_module, "get_sleep_metrics",
                          return_value={"red_hrv_streak": 0, "sleep_h": 7.5, "rhr_delta": 0}), \
             patch.object(app_module, "get_today_metrics",
                          return_value={"ctl": 50, "atl": 45, "tsb": 5}), \
             patch.object(app_module.db, "query_activities", return_value=[]):
            client = TestClient(app_module.app)
            self.assertEqual(client.post("/api/rides/sync").json()["plan_adapted"], "deload_advanced")
            d = client.get("/api/today-session").json()
        self.assertTrue(d.get("deload_advance"), "the card shows the chip")
        saved = json.loads((self._tmp / "current_plan.json").read_text())
        s = next(x for w in saved["weeks"] for x in w["sessions"] if x["day"] == tue.isoformat())
        self.assertEqual(d["planned"]["zwo_file"], s.get("zwo_file") or None)


class TestDeloadAdvance(DeloadAdvanceBase):
    def test_monotony_trip_converts_current_week(self):
        plan = _mk_continuous_plan(self.today)
        original = json.loads(json.dumps(plan["weeks"][0]))
        chip, path = self._advance(plan, _monotone_rides(self.today))

        self.assertIsNotNone(chip, "monotony >= 2.0 must advance the deload")
        self.assertEqual(chip["trigger"]["trigger"], "monotony")
        self.assertGreaterEqual(chip["trigger"]["value"], 2.0)
        self.assertEqual(chip["week_num"], 1)
        self.assertFalse(chip["reverted"])
        self.assertTrue(chip["reason"])
        self.assertNotIn("original_week", chip, "chip payload stays lean")

        saved = json.loads(path.read_text())
        wk = saved["weeks"][0]
        self.assertTrue(wk["is_stepback"])
        # ×0.72 Issurin unload band via plan_week
        self.assertEqual(wk["tss_target"], round(400 * 0.72))
        today_iso = self.today.isoformat()
        for s in wk["sessions"]:
            if s["day"] >= today_iso:
                self.assertNotIn(s["session_type"], HARD_TYPES,
                                 f"deload day {s['day']} still hard: {s['session_type']}")
        # Past days stay verbatim (frozen); rest days never gain a ride.
        by_day = {s["day"]: s for s in original["sessions"]}
        for s in wk["sessions"]:
            if s["day"] < today_iso:
                self.assertEqual(s["session_type"], by_day[s["day"]]["session_type"])
            if by_day[s["day"]]["session_type"] == "rest":
                self.assertEqual(s["session_type"], "rest")
        # Revertible record with the full snapshot persisted.
        rec = saved["deload_advance"]
        self.assertEqual(rec["week_num"], 1)
        self.assertEqual(rec["original_week"], original)
        self.assertEqual(rec["refit_days"], chip["refit_days"])

    def test_acwr_trip_converts_current_week(self):
        # Plan started last week; last week fully completed at 2× its target.
        plan = _mk_continuous_plan(self.today, flags=(False, False, False, True),
                                   weeks_back=1)
        plan["weeks"][0]["tss_target"] = 100
        prev_monday = date.fromisoformat(plan["weeks"][0]["start"])
        rides = [{"started_at": f"{(prev_monday + timedelta(days=i)).isoformat()}T09:00:00",
                  "tss": t, "time_in_zone": {"z2": 1800}}
                 for i, t in [(0, 100), (2, 60), (4, 40)]]
        chip, path = self._advance(plan, rides)
        self.assertIsNotNone(chip, "ACWR 2.0 > 1.5 must advance the deload")
        self.assertEqual(chip["trigger"]["trigger"], "acwr")
        self.assertEqual(chip["week_num"], 2)
        saved = json.loads(path.read_text())
        self.assertTrue(saved["weeks"][1]["is_stepback"])
        self.assertFalse(saved["weeks"][0]["is_stepback"],
                         "the completed week is history — never rewritten")

    def test_calm_series_never_trips(self):
        plan = _mk_continuous_plan(self.today)
        before = json.dumps(plan["weeks"])
        chip, path = self._advance(plan, _calm_rides(self.today))
        self.assertIsNone(chip)
        self.assertEqual(json.dumps(json.loads(path.read_text())["weeks"]), before,
                         "no trip → plan untouched")

    def test_never_two_deloads_back_to_back(self):
        rides = _monotone_rides(self.today)
        # (a) current week already a deload → no-op
        plan = _mk_continuous_plan(self.today, flags=(True, False, False, False))
        chip, _ = self._advance(plan, rides)
        self.assertIsNone(chip)
        # (b) the week just ridden was the deload → no-op
        plan = _mk_continuous_plan(self.today, flags=(True, False, False, False),
                                   weeks_back=1)
        chip, _ = self._advance(plan, rides)
        self.assertIsNone(chip)
        # (c) NEXT week is the scheduled deload (relief ≤7d out) → no-op
        plan = _mk_continuous_plan(self.today, flags=(False, True, False, False))
        chip, _ = self._advance(plan, rides)
        self.assertIsNone(chip)

    def test_advance_latches_idempotent(self):
        plan = _mk_continuous_plan(self.today)
        rides = _monotone_rides(self.today)
        chip1, path = self._advance(plan, rides)
        self.assertIsNotNone(chip1)
        saved1 = path.read_text()
        # Second check: same chip surfaces, NOTHING is rewritten.
        plan2 = json.loads(saved1)
        with patch.object(app_module, "_load_all_rides_safe", return_value=rides):
            chip2 = app_module._maybe_advance_continuous_deload(
                plan2, path, today=self.today)
        self.assertEqual(chip2, chip1)
        self.assertEqual(path.read_text(), saved1)

    def test_finite_goal_paths_untouched(self):
        # The check is only ever invoked for continuous plans; even called
        # bare on a finite plan dict it must not write (guard belt).
        plan = _mk_continuous_plan(self.today)
        plan["goal"]["type"] = "general"
        path = self._write(plan)
        with patch.object(app_module, "_load_all_rides_safe",
                          return_value=_monotone_rides(self.today)):
            self.assertFalse(app_module._plan_is_continuous(plan))


class TestDeloadRevert(DeloadAdvanceBase):
    def _advanced_plan(self):
        plan = _mk_continuous_plan(self.today)
        original = json.loads(json.dumps(plan["weeks"][0]))
        chip, path = self._advance(plan, _monotone_rides(self.today))
        assert chip is not None
        return original, path

    def test_revert_restores_remaining_days_and_latches(self):
        original, path = self._advanced_plan()
        client = TestClient(app_module.app)
        r = client.post("/api/plan/continuous/deload-revert")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["reverted"])

        saved = json.loads(path.read_text())
        wk = saved["weeks"][0]
        self.assertFalse(wk["is_stepback"])
        self.assertEqual(wk["tss_target"], original["tss_target"])
        by_day = {s["day"]: s for s in original["sessions"]}
        today_iso = self.today.isoformat()
        # The plan self-heal runs after every plan write (P8, v3.12.0) and
        # gives the restored blank fixture sessions a workout file; the
        # snapshot restore is judged on everything else.
        healed = {"zwo_file", "zwo_name", "matched"}
        for s in wk["sessions"]:
            if s["day"] >= today_iso:
                self.assertEqual({k: v for k, v in s.items() if k not in healed},
                                 {k: v for k, v in by_day[s["day"]].items() if k not in healed},
                                 "remaining days must restore the snapshot")
        rec = saved["deload_advance"]
        self.assertTrue(rec["reverted"])

        # Latch: the same week can NOT re-trigger after a revert.
        with patch.object(app_module, "_load_all_rides_safe",
                          return_value=_monotone_rides(self.today)):
            chip = app_module._maybe_advance_continuous_deload(
                saved, path, today=self.today)
        self.assertIsNone(chip)

        # Second revert is a clean no-op.
        r2 = client.post("/api/plan/continuous/deload-revert")
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.json()["reverted"])

    def test_revert_without_advance_is_noop(self):
        self._write(_mk_continuous_plan(self.today))
        r = TestClient(app_module.app).post("/api/plan/continuous/deload-revert")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["reverted"])
        self.assertEqual(r.json()["reason"], "nothing_to_revert")


# ═══════════════════════════════════════════════════════════════════════════
# D — /api/today-session exposure (additive for continuous, absent for finite)
# ═══════════════════════════════════════════════════════════════════════════

class TodaySessionBase(unittest.TestCase):
    SLEEP = {"red_hrv_streak": 0, "ln_rmssd_7d": 3.90, "swc_lower": 3.70,
             "swc_upper": 4.10, "sleep_h": 7.5, "rhr_delta": 0}
    TRAINING = {"ctl": 50, "atl": 45, "tsb": 5}

    def setUp(self):
        app_module.clear_cache()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp
        self.today = date.today()
        self._patches = [
            patch.object(app_module, "_maybe_lazy_icu_sync", return_value=None),
            patch.object(app_module.db, "query_activities", return_value=[]),
            patch.object(app_module, "get_sleep_metrics",
                         return_value=dict(self.SLEEP)),
            patch.object(app_module, "get_today_metrics",
                         return_value=dict(self.TRAINING)),
        ]
        for p in self._patches:
            p.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _get(self, rides):
        with patch.object(app_module, "_load_all_rides_safe", return_value=rides):
            r = self.client.get("/api/today-session")
        assert r.status_code == 200, r.text
        return r.json()

    def _sync(self, rides):
        """The dashboard's POST /api/rides/sync, where the deload advance runs
        since 2026-09-14 (a GET used to run it). No ICU: nothing new arrives."""
        with patch.object(app_module, "_load_all_rides_safe", return_value=rides), \
             patch.object(app_module, "_sync_icu_activities",
                          return_value={"added": 0, "updated": 0, "status": "no_credentials"}):
            r = self.client.post("/api/rides/sync")
        assert r.status_code == 200, r.text
        return r.json()


class TestTodaySessionContinuousFields(TodaySessionBase):
    def test_continuous_goal_carries_suggestion(self):
        plan = _mk_continuous_plan(self.today)
        (self._tmp / "current_plan.json").write_text(json.dumps(plan))
        data = self._get(_calm_rides(self.today))
        self.assertIn("continuous_suggestion", data)
        sug = data["continuous_suggestion"]
        self.assertEqual(set(sug), {"family", "goal_zone", "session_type", "reason"})
        self.assertIn(sug["family"], cpol.FAMILIES)
        # H1: the headline is the session the rider is told to ride
        eff = data["adjusted"]["session_type"]
        self.assertEqual(sug["session_type"], eff)
        self.assertTrue(sug["reason"])
        # calm rides → no deload chip
        self.assertNotIn("deload_advance", data)
        # existing shape intact (no breaking change)
        self.assertIn("planned", data)
        self.assertIn("adjusted", data)

    def test_finite_goal_has_no_continuous_fields(self):
        plan = _mk_continuous_plan(self.today)
        plan["goal"] = {"type": "general", "hours_per_week": 8.0,
                        "rest_days": [0, 4]}
        (self._tmp / "current_plan.json").write_text(json.dumps(plan))
        data = self._get(_calm_rides(self.today))
        self.assertNotIn("continuous_suggestion", data)
        self.assertNotIn("deload_advance", data)

    def test_monotony_trip_surfaces_deload_chip(self):
        plan = _mk_continuous_plan(self.today)
        path = self._tmp / "current_plan.json"
        path.write_text(json.dumps(plan))
        # A read does not advance it...
        self.assertNotIn("deload_advance", self._get(_monotone_rides(self.today)))
        self.assertFalse(json.loads(path.read_text())["weeks"][0]["is_stepback"])
        # ...the sync does, and says so; the card then shows the chip.
        self.assertEqual(self._sync(_monotone_rides(self.today))["plan_adapted"], "deload_advanced")
        data = self._get(_monotone_rides(self.today))
        self.assertIn("deload_advance", data)
        chip = data["deload_advance"]
        self.assertEqual(chip["trigger"]["trigger"], "monotony")
        self.assertFalse(chip["reverted"])
        self.assertTrue(chip["reason"])
        # ... and the stored plan was actually converted.
        saved = json.loads(path.read_text())
        self.assertTrue(saved["weeks"][0]["is_stepback"])
        self.assertEqual(saved["deload_advance"]["week_num"], chip["week_num"])


# ═══════════════════════════════════════════════════════════════════════════
# focus plumbing (W2 API plumbing for the W1 engine field)
# ═══════════════════════════════════════════════════════════════════════════

def test_goal_from_plan_dict_carries_focus():
    g = app_module._goal_from_plan_dict(
        {"type": "continuous", "focus": "vo2", "hours_per_week": 8.0})
    assert g.goal_type == "continuous" and g.focus == "vo2"
    # default + finite goals stay harmless
    g2 = app_module._goal_from_plan_dict({"type": "general"})
    assert g2.focus == "both"
