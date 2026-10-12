"""Today's headline follows today's session (contract slim/9.3, H1-H3).

The owner's report (2026-10-12): the dashboard said "High-aerobic today ...
Z3-Z5 still owed 39min this week" over a planned recovery Monday. The
headline never read today's session; it subtracted the week's ridden Z3-Z5
minutes from the planned ones, so on Monday every hard minute of the week
was owed today, though Tuesday and Thursday held them.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import clock
import week_view as wv

MON = date(2026, 10, 12)


def _s(day, st, zm):
    return {"day": day.isoformat(), "session_type": st,
            "goal_zone": 3 if st == "vo2max" else 1,
            "zone_minutes": dict(zip(wv.ZONES, zm))}


WEEK = [_s(MON, "recovery", (61, 0, 0)), _s(MON + timedelta(1), "vo2max", (55, 0, 21)),
        _s(MON + timedelta(2), "z2", (180, 0, 0)), _s(MON + timedelta(3), "vo2max", (42, 0, 18))]


def test_monday_owes_nothing_the_week_still_holds():
    owed = wv.zone_owed(WEEK, {z: 0 for z in wv.ZONES}, MON, rode_today=False)
    assert owed == {"zone1": 0, "zone2": 0, "zone3": 0}


def test_a_missed_day_is_owed_and_a_ridden_one_is_not():
    wed = MON + timedelta(2)
    owed = wv.zone_owed(WEEK, {"zone1": 61, "zone2": 0, "zone3": 0}, wed, rode_today=False)
    assert owed["zone3"] == 21                    # Tuesday's VO2 work was not ridden
    owed = wv.zone_owed(WEEK, {"zone1": 116, "zone2": 0, "zone3": 21}, wed, rode_today=False)
    assert owed["zone3"] == 0
    # today ridden: its minutes are in `ridden`, not held as well
    owed = wv.zone_owed(WEEK, {"zone1": 61, "zone2": 0, "zone3": 21}, MON + timedelta(1),
                        rode_today=True)
    assert owed["zone3"] == 0


class TestTheOwnersMonday(unittest.TestCase):
    """End to end on /api/today-session: a continuous plan, recovery Monday,
    VO2max Tuesday and Thursday, no wellness."""

    def setUp(self):
        app_module.clear_cache()
        self.addCleanup(app_module.clear_cache)
        clock.freeze(MON)
        self.addCleanup(clock.unfreeze)
        tmp = Path(tempfile.mkdtemp(prefix="one_headline_"))
        day = lambda i: (MON + timedelta(days=i)).isoformat()  # noqa: E731
        sess = [
            (0, "recovery", 61, "z2_steady_54pct_61min.zwo"),
            (1, "vo2max", 76, "vo2_short_4x10x30s-30s_120pct_76min.zwo"),
            (2, "z2", 60, ""),
            (3, "vo2max", 60, "vo2_short_2x10x30s_112pct_60min.zwo"),
        ]
        self.plan_path = tmp / "current_plan.json"
        (tmp / "current_plan.json").write_text(json.dumps({
            "goal": {"type": "continuous", "focus": "both", "hours_per_week": 8.0},
            "phases": [],
            "weeks": [{"week_num": 1, "start": day(0), "end": day(6), "phase": "continuous",
                       "tss_target": 400, "is_stepback": False,
                       "sessions": [{"day": day(i), "day_name": "", "session_type": st,
                                     "duration_min": dur, "tss_estimate": 50, "description": "",
                                     "zwo_file": zf, "zwo_name": "", "status": "pending"}
                                    for i, st, dur, zf in sess]}]}))
        self.rides = []
        for p in (patch.object(app_module, "_plan_dir", return_value=tmp),
                  patch.object(app_module, "get_today_metrics", return_value={"tsb": 5.0}),
                  patch.object(app_module, "get_sleep_metrics", return_value={}),
                  patch.object(app_module, "_local_sleep_metrics", return_value={}),
                  patch.object(app_module, "_load_all_rides_safe", side_effect=lambda: list(self.rides)),
                  patch.object(app_module, "_maybe_lazy_icu_sync", return_value=None),
                  patch.object(app_module.db, "query_activities", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app_module.app)

    def _headline(self):
        d = self.client.get("/api/today-session").json()
        return d, d["continuous_suggestion"]

    def test_recovery_monday_reads_recovery(self):
        d, h = self._headline()
        self.assertEqual(d["adjusted"]["session_type"], "recovery")
        self.assertEqual((h["family"], h["goal_zone"], h["session_type"]), ("zone_1", 1, "recovery"))
        self.assertEqual(h["reason"], "Recovery (zone 1) today, as planned. No HRV baseline yet.")

    def test_vo2max_tuesday_reads_zone_3_and_owes_nothing(self):
        clock.freeze(MON + timedelta(1))
        self.rides = [{"started_at": f"{MON.isoformat()}T10:00:00", "tss": 40,
                       "time_in_zone": {"z1": 2400, "z2": 1260}}]
        _d, h = self._headline()
        self.assertEqual((h["family"], h["session_type"]), ("zone_3", "vo2max"))
        self.assertTrue(h["reason"].startswith("VO2max (zone 3) today, as planned."), h["reason"])
        self.assertNotIn("owed", h["reason"])

    def test_wednesday_after_a_missed_tuesday_owes_its_zone_3(self):
        clock.freeze(MON + timedelta(2))
        _d, h = self._headline()
        self.assertEqual(h["family"], "zone_1")
        self.assertIn("Zone 3 still owed", h["reason"])
        # a ridden Tuesday, the session by its completion match (its VO2 work
        # counted to zone 3 by the hybrid), pays it
        self.rides = [{"ride_id": "icu_i5", "started_at": f"{(MON + timedelta(1)).isoformat()}T10:00:00",
                       "tss": 70, "time_in_zone": {"z1": 2400, "z2": 900, "z4": 300, "z5": 600, "z6": 360}}]
        plan = json.loads(self.plan_path.read_text())
        tue = plan["weeks"][0]["sessions"][1]
        tue.update(status="done", completion_matches=[{"activity_id": "i5"}])
        self.plan_path.write_text(json.dumps(plan))
        app_module.clear_cache()
        _d, h = self._headline()
        self.assertNotIn("owed", h["reason"])
        # the same ride with no match is not the session: time in zone, owed
        tue.update(status="pending", completion_matches=None)
        self.plan_path.write_text(json.dumps(plan))
        app_module.clear_cache()
        _d, h = self._headline()
        self.assertIn("Zone 3 still owed", h["reason"])
        tue.update(status="done", completion_matches=[{"activity_id": "i5"}])
        self.plan_path.write_text(json.dumps(plan))
        app_module.clear_cache()
        _d, h = self._headline()
        self.assertNotIn("owed", h["reason"])


def test_an_adjusted_day_holds_the_adjusted_session_not_the_planned_minutes():
    """Today's VO2max eased to Z2: today holds Z2 minutes, so the VO2 work it
    no longer carries is owed (review of 82b17875)."""
    tue = (MON + timedelta(1))
    week = {"start": MON.isoformat(), "sessions": [
        dict(s, goal_zone=3 if s["session_type"] == "vo2max" else 1) for s in WEEK]}
    plan = {"weeks": [{"start": MON.isoformat(), "end": (MON + timedelta(6)).isoformat()}]}
    h = app_module._continuous_today_suggestion(
        plan, {}, {"tsb": 5.0}, [], week, effective_type="z2", planned_type="vo2max",
        adjustment_reason="HRV below SWC day 1", effective_duration=76, today=tue)
    assert h["family"] == "zone_1"
    assert "Zone 3 still owed 21min" in h["reason"], h["reason"]


def test_a_missed_easy_days_surges_are_not_owed():
    """SF2 (evaluation of 2382cfd1): 10-29 reported zone 2 owed from the
    tempo pickups of missed endurance days. Only the planned hard sessions'
    hard-zone minutes are a debt."""
    week = [_s(MON, "z2", (110, 8, 0)), _s(MON + timedelta(1), "z2", (40, 5, 0))]
    owed = wv.zone_owed(week, {z: 0 for z in wv.ZONES}, MON + timedelta(3), rode_today=False)
    assert owed == {"zone1": 0, "zone2": 0, "zone3": 0}


def test_no_owed_note_on_a_rest_or_recovery_day():
    import continuous_policy as cpol
    owed = {"zone2": 40, "zone3": 21}
    for st, goal, planned in (("rest", None, None), ("recovery", 1, None),
                              ("recovery", 1, "vo2max")):   # forced recovery (G5)
        h = cpol.today_headline(st, goal, {"tsb": 5}, None, owed=owed, planned_type=planned,
                                adjustment_reason="G5 soreness 6/7")
        assert "owed" not in h["reason"], (st, h["reason"])
    assert "owed" in cpol.today_headline("z2", 1, {"tsb": 5}, None, owed=owed)["reason"]
