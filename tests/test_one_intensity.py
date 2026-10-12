"""Every surface counts intensity the same (contract slim/9.3, I1-I2).

The owner's week of 2026-10-12, measured: week view low/mid+high/anaerobic
518/1/38 min, calendar Z1Z2/Z3Z4/Z5+ 496/14/48, headline "Z3-Z5 owed 39":
three counts of one week. The week view banded each file's zone shares into
four bands of its own, the calendar used its own thresholds and a type
heuristic (a VO2max session without a file was 55/10/35), the week summary a
third table. Now every one reads one count: planned files and rides with
power by time in zone (zones.tiz_minutes, owner's amendment 2), a ride with
heart rate only by the hybrid (zones.hr_hybrid_minutes) for
the goal zone of the session planned on its day) and names the planner's three
zones.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import clock
import download_lib
import week_view as wv
import zones

MON = date(2026, 10, 12)
VO2 = "vo2_short_4x10x30s-30s_120pct_76min.zwo"
Z2 = "z2_steady_54pct_61min.zwo"
WORKOUTS = Path(__file__).resolve().parent.parent / "src" / "workouts"
DASH = (Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html").read_text()


class _Week(unittest.TestCase):
    """Mon recovery (file), Tue VO2max (file), Wed VO2max with no file, Thu
    rest; a ride on Tuesday. Today is Wednesday."""

    def setUp(self):
        app_module.clear_cache()
        self.addCleanup(app_module.clear_cache)
        clock.freeze(MON + timedelta(days=2))
        self.addCleanup(clock.unfreeze)
        tmp = Path(tempfile.mkdtemp(prefix="one_intensity_"))
        d = lambda i: (MON + timedelta(days=i)).isoformat()  # noqa: E731
        sess = [(0, "recovery", 61, Z2), (1, "vo2max", 76, VO2), (2, "vo2max", 60, ""),
                (3, "rest", 0, "")]
        (tmp / "current_plan.json").write_text(json.dumps({
            "goal": {"type": "general", "hours_per_week": 8.0}, "phases": [],
            "weeks": [{"week_num": 1, "start": d(0), "end": d(6), "phase": "build",
                       "tss_target": 400, "is_stepback": False,
                       "sessions": [{"day": d(i), "day_name": "", "session_type": st,
                                     "duration_min": dur, "tss_estimate": 50 if dur else 0,
                                     "description": "", "zwo_file": zf, "zwo_name": "",
                                     "status": "done" if i == 1 else "pending",
                                     # the completion match says the ride IS Tuesday's
                                     # session (both id spellings the tests use)
                                     "completion_matches": ([{"activity_id": "77"}, {"activity_id": "i77"}]
                                                            if i == 1 else None)}
                                    for i, st, dur, zf in sess]}]}))
        tiz = {"z1": 2400, "z2": 900, "z3": 120, "z4": 180, "z5": 600, "z6": 360, "z7": 0}
        self.ride = {"ride_id": "icu_77", "source": "icu", "sport": "Ride", "name": "VO2",
                     "started_at": f"{d(1)}T10:00:00", "date": d(1), "tss": 80,
                     "moving_time_s": 4560, "duration_s": 4560, "time_in_zone": tiz}
        act = [{"id": "77", "date": d(1), "sport": "Ride", "duration_min": 76, "tss": 80,
                "avg_hr": 150}]
        for p in (patch.object(app_module, "_plan_dir", return_value=tmp),
                  patch.object(app_module, "_load_all_rides_safe", return_value=[self.ride]),
                  patch.object(app_module, "_load_full_ride", side_effect=lambda r: r),
                  patch.object(app_module, "api_activities", return_value=act),
                  patch("ride_storage.list_rides", return_value=[]),
                  patch.object(app_module, "get_today_metrics", return_value={}),
                  patch.object(app_module, "get_sleep_metrics", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app_module.app)

    def _get(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TestTheWeekViewCountsByTimeInZone(_Week):

    def test_sessions_carry_their_goal_zone_and_time_in_zone(self):
        wk = self._get("/api/weekly-plan")
        by = {s["day"]: s for s in wk["sessions"]}
        tue = by[(MON + timedelta(1)).isoformat()]
        self.assertEqual(tue["goal_zone"], 3)
        served = download_lib.served_zone_minutes(WORKOUTS / VO2, 76, "vo2max")
        self.assertEqual(tue["zone_minutes"], {f"zone{i}": int(round(served[f"z{i}"])) for i in (1, 2, 3)})
        tiz = zones.tiz_minutes((WORKOUTS / VO2).read_bytes())
        self.assertEqual(served, tiz)                 # every second, no goal-zone credit
        # a VO2max session with no file counts its duration to its goal zone
        wed = by[(MON + timedelta(2)).isoformat()]
        self.assertEqual(wed["zone_minutes"], {"zone1": 0, "zone2": 0, "zone3": 60})
        # the fileless VO2max day is a session-goal count and says so (SF4)
        self.assertEqual(tue["zone_method"], "time in zone")
        self.assertEqual(wed["zone_method"], "by type")
        self.assertEqual(wk["zone_method"], "time in zone, 1 by type")
        self.assertNotIn("exposure_minutes_planned", wk)

    def test_the_power_hybrid_is_gone(self):
        # amendment 2: power has no lag to correct, no goal-zone credit
        self.assertFalse(hasattr(zones, "hybrid_minutes"))
        self.assertFalse(hasattr(zones, "hybrid_ride_minutes"))


class TestEverySurfaceCountsTheSame(_Week):

    def test_planned_minutes_per_zone_agree(self):
        wk = self._get("/api/weekly-plan")
        ws = self._get("/api/week-summary")
        cal = self._get("/api/calendar")
        cw = next(w for w in cal["weeks"] if w["start_date"] == MON.isoformat())
        self.assertEqual(ws["zone_minutes_planned"], wk["zone_minutes_planned"])
        for z in wv.ZONES:
            self.assertAlmostEqual(cw[f"planned_{z}_min"], wk["zone_minutes_planned"][z], delta=1.0)
        self.assertEqual(cw["zone_method"], "time in zone, 1 by type")
        self.assertEqual(ws["zone_method"], wk["zone_method"])
        wed = next(dd for dd in cw["days"] if dd["date"] == (MON + timedelta(2)).isoformat())
        self.assertEqual(wed["planned"]["zone_method"], "by type")
        # the VO2max day without a file: 60 min of zone 3, not 55/10/35
        self.assertGreaterEqual(cw["planned_zone3_min"], 60)

    def test_ridden_minutes_per_zone_agree_and_follow_the_days_session(self):
        ws = self._get("/api/week-summary")
        cal = self._get("/api/calendar")
        cw = next(w for w in cal["weeks"] if w["start_date"] == MON.isoformat())
        # Tuesday's ride has power: time in zone, every second, matched or
        # not: 40 + 15 min zone 1, 2 + 3 min zone 2, 10 + 6 min zone 3
        want = {"zone1": 55, "zone2": 5, "zone3": 16}
        self.assertEqual(ws["zone_minutes"], want)
        for z in wv.ZONES:
            self.assertAlmostEqual(cw[f"actual_{z}_min"], want[z], delta=0.5)
        day = next(dd for dd in cw["days"] if dd["date"] == (MON + timedelta(1)).isoformat())
        self.assertEqual(round(day["actual"]["zone3_min"]), 16)
        self.assertEqual(day["actual"]["zone_method"], "time in zone")
        self.assertEqual((ws["zone_method_ridden"], cw["zone_method_ridden"]),
                         ("time in zone", "time in zone"))
        self.assertNotIn("z5plus_min", day["actual"])


def test_the_dashboard_reads_no_retired_field():
    """Field renames need every reader updated: no dashboard read of the
    Coggan-grouped or four-band names is left."""
    retired = re.compile(r"exposure_minutes|z1z2_min|z3z4_min|z5plus_min|z4plus_min|"
                         r"realized_bands|\b(low|mid|high)_aerobic\b")
    assert retired.findall(DASH) == []


def test_every_distribution_figure_says_its_method():
    for needle in ("ridden: ${esc(d.zone_method_ridden || 'time in zone')}",
                   "ridden: ${esc(s.zone_method_ridden || 'time in zone')}",
                   "ridden: ${esc(w.zone_method_ridden || 'time in zone')}",
                   "`(${idist.method || 'time in zone'})`",
                   "Polarization index <span style=\"font-weight:400;color:var(--text3);\">(time in zone)</span>",
                   "PI = ${piVal} · Treff 2019 · time in zone",
                   "`Realized mix (${d.plan_json.realized_zones.method || 'time in zone'}): `"):
        assert needle in DASH, needle


class TestWeekSummaryFindsEachRidesTimeInZone(_Week):
    """/api/activities ids from the ride store are "icu_i..." while the store
    is keyed "icu_..." too, or bare from intervals.icu: the week summary found
    a ride's time in zone only for the bare spelling, and counted every other
    ride by its average HR while the card said "hybrid" (review of 82b17875)."""

    def test_both_id_spellings_reach_the_ride_record(self):
        d = (MON + timedelta(1)).isoformat()
        for rid, aid in (("icu_i77", "icu_i77"), ("icu_i77", "i77")):
            ride = dict(self.ride, ride_id=rid)
            act = [{"id": aid, "date": d, "sport": "Ride", "duration_min": 76, "tss": 80,
                    "avg_hr": 150}]
            app_module.clear_cache()
            with patch.object(app_module, "_load_all_rides_safe", return_value=[ride]), \
                 patch.object(app_module, "api_activities", return_value=act):
                ws = self._get("/api/week-summary")
            self.assertEqual(ws["zone_minutes"], {"zone1": 55, "zone2": 5, "zone3": 16}, aid)
            self.assertEqual(ws["activities_by_day"][d][0]["zone_signal"], "time_in_zone", aid)


class TestARideWithoutPowerZonesIsCountedTheSameEverywhere(_Week):
    """A mountain-bike ride with HR and no power time in zone: the week
    summary counted its minutes by average HR, the calendar counted none
    (the owner's 2026-10-06, found by the probe's ridden-minutes rule)."""

    def test_hr_fallback_on_both_surfaces(self):
        d = (MON + timedelta(1)).isoformat()
        mtb = {"ride_id": "icu_88", "source": "icu", "sport": "MountainBikeRide",
               "started_at": f"{d}T10:00:00", "date": d, "tss": 50, "avg_hr": 130,
               "moving_time_s": 3600, "duration_s": 3600, "time_in_zone": {}}
        act = [{"id": "icu_88", "date": d, "sport": "MountainBikeRide", "duration_min": 60,
                "tss": 50, "avg_hr": 130}]
        with patch.object(app_module, "_load_all_rides_safe", return_value=[mtb]), \
             patch.object(app_module, "api_activities", return_value=act), \
             patch.object(app_module.config, "ATHLETE_LTHR", 170):
            ws = self._get("/api/week-summary")
            cal = self._get("/api/calendar")
        cw = next(w for w in cal["weeks"] if w["start_date"] == MON.isoformat())
        self.assertEqual(ws["zone_minutes"]["zone1"], 60)
        self.assertAlmostEqual(cw["actual_zone1_min"], 60, delta=0.5)


class TestAnHrOnlyRideTakesAGoalOnlyThroughItsMatch(_Week):
    """Amendments 1 and 2: the hybrid is for rides with heart rate only, and a
    ride takes a session's goal zone only through its completion match; a
    second HR-only ride on the VO2max day (a commute) is HR time in zone."""

    def test_hr_only_rides_matched_and_unmatched(self):
        d = (MON + timedelta(1)).isoformat()
        hr = {"z1": 900, "z2": 600, "z3": 0, "z4": 240, "z5": 60, "z6": 0, "z7": 0}
        vo2 = dict(self.ride, time_in_zone={}, hr_time_in_zone=dict(hr))   # matched (i77)
        commute = {"ride_id": "icu_99", "source": "icu", "sport": "Ride", "name": "commute",
                   "started_at": f"{d}T18:00:00", "date": d, "tss": 20, "moving_time_s": 1800,
                   "duration_s": 1800, "time_in_zone": {}, "hr_time_in_zone": dict(hr)}
        act = [{"id": "77", "date": d, "sport": "Ride", "duration_min": 76, "tss": 80},
               {"id": "99", "date": d, "sport": "Ride", "duration_min": 30, "tss": 20}]
        with patch.object(app_module, "_load_all_rides_safe", return_value=[vo2, commute]), \
             patch.object(app_module, "api_activities", return_value=act):
            ws = self._get("/api/week-summary")
            cal = self._get("/api/calendar")
        # the matched VO2 ride 25/0/5 (hybrid (HR)) + the commute 25/4/1 (HR
        # time in zone)
        want = {"zone1": 50, "zone2": 4, "zone3": 6}
        self.assertEqual(ws["zone_minutes"], want)
        self.assertEqual(ws["zone_method_ridden"], "hybrid (HR), 1 HR time in zone")
        cw = next(w for w in cal["weeks"] if w["start_date"] == MON.isoformat())
        for z in wv.ZONES:
            self.assertAlmostEqual(cw[f"actual_{z}_min"], want[z], delta=0.6)


def test_a_rides_hr_zones_come_from_its_icu_activity():
    """_ride_count reads the ride's HR zone boundaries from the activity
    intervals.icu sent (icu_hr_zones, lthr in raw_json)."""
    import json as _json
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE activities (id TEXT PRIMARY KEY, raw_json TEXT)")
    conn.execute("INSERT INTO activities VALUES (?, ?)",
                 ("i5", _json.dumps({"icu_hr_zones": [148, 165, 174, 183, 193], "lthr": 175})))
    ride = {"ride_id": "icu_i5", "time_in_zone": {},
            "hr_time_in_zone": {"z1": 600, "z2": 600, "z3": 600, "z4": 600, "z5": 600}}
    app_module.clear_cache()
    try:
        with patch.object(app_module.db, "get_db", return_value=conn):
            m, how = app_module._ride_count(ride, None)
    finally:
        app_module.clear_cache()
    assert how == "HR time in zone"
    # its z4 (175-183 bpm) is above LTHR: zone 3 (Friel's seven would say 20/20/10)
    assert {k: round(v) for k, v in m.items()} == {"zone1": 20, "zone2": 10, "zone3": 20}
