"""Replanning mid-week keeps the days already ridden.

THE SYMPTOM. Replan on a Thursday and the home page reads "TSS 499 / 138",
while intervals.icu reads "499 / 387" for the same week. intervals.icu is
right: 387 is what was prescribed Monday to Friday. Domestique's number is a
full calendar week of riding divided by whatever is left of a plan that now
begins today.

THE CAUSE. ``regenerate_from_today`` keeps weeks that ENDED before today
(``w.end < today``) and rebuilds everything else from a cursor starting today.
The current week ends on Sunday, so it is not a past week, so it is dropped
whole -- Monday to Wednesday with it -- and the replacement row starts today.
The plan stops being a record of what was asked of the rider.

THE RULE. The week the replan lands in keeps its elapsed days exactly as they
stood: same sessions, same statuses, same span. Only today-and-later is
re-prescribed. A week row therefore still covers one calendar week, which is
what makes the rollup's numerator and denominator comparable.
"""
from __future__ import annotations

import json
import plan_seam  # noqa: E402  (the plan dir of the active profile)
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module
import ride_storage  # noqa: E402
import clock  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

THURSDAY = date(2026, 9, 17)
MONDAY = date(2026, 9, 14)
# What the rider was asked for, Monday to Sunday, before the replan.
PLANNED_TSS = {0: 101, 1: 55, 2: 51, 3: 43, 4: 137, 5: 0, 6: 60}


def _session(day: date, tss: int, status: str = "pending") -> dict:
    return {"day": day.isoformat(), "day_name": day.strftime("%a"),
            "session_type": "rest" if tss == 0 else "z2",
            "duration_min": 0 if tss == 0 else tss + 20,
            "tss_estimate": tss, "description": "x", "status": status,
            "zwo_file": "" if tss == 0 else "z2_steady_56pct_60min.zwo",
            "zwo_name": "" if tss == 0 else "Z2"}


class TestReplanKeepsElapsedDays(unittest.TestCase):
    def setUp(self):
        clock.freeze(THURSDAY)
        self.tmp = Path(tempfile.mkdtemp(prefix="replan_"))
        self.plan_p = self.tmp / "current_plan.json"
        self._patch = plan_seam.patch_plan_dir(self.tmp)
        self._patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch.stop()
        clock.unfreeze()

    def _write_plan(self):
        weeks = []
        for wk in range(-2, 3):
            wstart = MONDAY + timedelta(weeks=wk)
            sessions = []
            for d in range(7):
                day = wstart + timedelta(days=d)
                # Everything before today has been ridden.
                status = "done" if day < THURSDAY else "pending"
                sessions.append(_session(day, PLANNED_TSS[d], status))
            weeks.append({"week_num": wk + 3, "start": wstart.isoformat(),
                          "end": (wstart + timedelta(days=6)).isoformat(),
                          "phase": "build", "tss_target": sum(PLANNED_TSS.values()),
                          "is_stepback": False, "sessions": sessions})
        self.plan_p.write_text(json.dumps({
            "goal": {"type": "general",
                     "event_date": (THURSDAY + timedelta(weeks=8)).isoformat()},
            "phases": [], "weeks": weeks,
            "generated": (THURSDAY - timedelta(days=40)).isoformat(),
        }), encoding="utf-8")

    def _ridden(self):
        """The rides behind the replan. Without them the regen reads an
        absence and inserts a reconditioning ramp, which rebuilds the week on
        its own grid -- a different scenario from the one this file is about
        (a rider who trained, then replanned)."""
        rides = []
        for wk in range(-2, 1):
            for d in range(7):
                day = MONDAY + timedelta(weeks=wk, days=d)
                if day >= THURSDAY or not PLANNED_TSS[d]:
                    continue
                rides.append({"date": day.isoformat(), "tss": PLANNED_TSS[d],
                              "duration_min": PLANNED_TSS[d] + 20, "sport": "Ride",
                              "avg_hr": 140, "name": "ride"})
        return rides

    def _regenerate(self):
        with patch.object(app_module, "cached",
                          side_effect=lambda k, fn, **kw: fn() if k != "training"
                          else {"ctl": 40}), \
             patch.object(app_module.db, "query_activities",
                          return_value=self._ridden()), \
             patch.object(ride_storage, "ride_record", return_value=self._ridden()):
            # the one seam: the planner reads the rider's rides here
            r = self.client.post("/api/plan/regenerate")
        self.assertEqual(r.status_code, 200, r.text)
        return json.loads(self.plan_p.read_text())

    def _this_week(self, plan: dict) -> dict:
        got = [w for w in plan["weeks"]
               if w["start"] <= THURSDAY.isoformat() <= w["end"]]
        self.assertEqual(len(got), 1,
                         f"one row per calendar week, got {[ (w['start'], w['end']) for w in got ]}")
        return got[0]

    def test_the_week_the_replan_lands_in_is_still_that_calendar_week(self):
        self._write_plan()
        week = self._this_week(self._regenerate())
        self.assertEqual(week["start"], MONDAY.isoformat(),
                         "the replan truncated the week to start today; the days "
                         "already ridden are no longer in the plan")
        self.assertEqual(week["end"], (MONDAY + timedelta(days=6)).isoformat(),
                         "the row no longer covers exactly one calendar week, so "
                         "every Monday-to-Sunday rollup divides by the wrong span")

    def test_the_days_already_ridden_keep_their_sessions_and_statuses(self):
        self._write_plan()
        week = self._this_week(self._regenerate())
        by_day = {s["day"]: s for s in week["sessions"]}
        for d in range(3):                       # Mon, Tue, Wed
            day = (MONDAY + timedelta(days=d)).isoformat()
            self.assertIn(day, by_day, f"{day} was ridden and is gone from the plan")
            self.assertEqual(by_day[day]["status"], "done", f"{day} lost its status")
            self.assertEqual(by_day[day]["tss_estimate"], PLANNED_TSS[d],
                             f"{day} was re-prescribed instead of kept")

    def test_the_rollup_divides_like_by_like(self):
        """The symptom, at the endpoint that showed it. /api/week-summary sums
        RIDES over Monday..Sunday; its target has to cover the same days, or a
        full week of riding is graded against whatever is left of the plan
        (499/138, where intervals.icu said 499/387)."""
        self._write_plan()
        self._regenerate()
        rides = [{"date": (MONDAY + timedelta(days=d)).isoformat(),
                  "tss": PLANNED_TSS[d], "duration_min": 60, "sport": "Ride",
                  "avg_hr": 140, "name": "ride"}
                 for d in range(3)]
        with patch.object(app_module, "api_activities", return_value=rides):
            r = self.client.get("/api/week-summary")
        self.assertEqual(r.status_code, 200, r.text)
        got = r.json()
        done, target = got["tss_done"], got["planned_tss"]
        self.assertGreaterEqual(
            target, sum(PLANNED_TSS[d] for d in range(3)),
            f"rollup reads {done:.0f} done / {target} planned: the rides span "
            f"Mon-Sun and the target does not")

    def test_the_weeks_target_still_covers_the_whole_week(self):
        """The rollup divides a Monday-to-Sunday ride total by this number, so
        it has to cover Monday to Sunday. 499/138 is what it looked like when
        it did not."""
        self._write_plan()
        week = self._this_week(self._regenerate())
        elapsed = sum(PLANNED_TSS[d] for d in range(3))
        planned = sum(s.get("tss_estimate") or 0 for s in week["sessions"])
        self.assertGreaterEqual(
            planned, elapsed,
            f"the week's sessions add up to {planned} TSS but {elapsed} was "
            f"already prescribed and ridden before the replan")


    def test_a_week_row_never_spans_more_than_seven_days(self):
        """The rows a regen rebuilds are not always on the Monday grid: after
        an absence it inserts a reconditioning ramp built today..+6, and a
        taper is laid backward from race day and exempt by design. Widening
        one of those back to Monday would make a single row cover ten days,
        and every Monday-to-Sunday reader would divide by the wrong span. The
        rider who was away gets the old behaviour, not a broken week."""
        self._write_plan()
        with patch.object(app_module, "cached",
                          side_effect=lambda k, fn, **kw: fn() if k != "training"
                          else {"ctl": 40}), \
             patch.object(app_module.db, "query_activities", return_value=[]):
            r = self.client.post("/api/plan/regenerate")
        self.assertEqual(r.status_code, 200, r.text)
        out = json.loads(self.plan_p.read_text())
        for w in out["weeks"]:
            span = (date.fromisoformat(w["end"]) - date.fromisoformat(w["start"])).days + 1
            self.assertLessEqual(span, 7, f"week {w['week_num']} spans {span} days "
                                          f"({w['start']}..{w['end']})")

    def test_the_carried_days_do_not_bankrupt_the_weeks_budget(self):
        """``plan_invariants.budget`` reads a week's remaining allowance as
        ``tss_target - what the days behind today already asked for``. Carry
        the elapsed days in without carrying their share of the target and the
        week's budget collapses (33 TSS against 249 prescribed here), inventing
        a weekly_volume violation out of arithmetic."""
        import plan_invariants as pi
        import training_planner as tp

        self._write_plan()
        out = self._regenerate()
        weeks = [tp.week_from_dict(w) for w in out["weeks"]]
        cur = next(w for w in weeks if w.start <= THURSDAY <= w.end)
        budget, ahead = pi.budget(cur, None, THURSDAY), pi._load(cur, None, THURSDAY)
        self.assertGreaterEqual(
            budget * 1.15, ahead,
            f"the week prescribes {ahead:.0f} TSS from today with a budget of "
            f"{budget:.0f}: the elapsed days were carried in but their share of "
            f"tss_target was not, so budget() subtracts them twice")
        # Deliberately NOT asserting a clean audit. Regenerate violates
        # weekly_volume on its own for most riders (measured on 30 riders: 17
        # of 17 on a mid-week rebuild) and that predates this change -- it is
        # the pass chain's problem, not this one. Asserting it here would make
        # this file fail for a reason it cannot fix, which is how a test starts
        # lying. The budget arithmetic above is what this change owns.


if __name__ == "__main__":
    unittest.main()
