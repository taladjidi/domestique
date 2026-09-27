"""Adherence compares the same days on both sides.

REPORTED. The home page read "TSS 499 / 138" for a week intervals.icu read as
499/387. The rider had replanned on Thursday, so the plan covered Friday to
Sunday while the ride total covered Monday to Sunday: a full week of riding
divided by three days of plan. Both numbers were true and the ratio was
meaningless.

A plan does not always cover a calendar week. It can start mid-week (a replan,
a first plan, an entry from a past date), end mid-week (the last week before an
event), or cover a single day. The week's ride total is still worth showing --
it is what the rider did with their week -- but the GRADE has to be taken over
the days the plan actually covers.

  tss_done            what the rider rode, Monday to Sunday
  tss_done_in_plan    what they rode on the days the plan covers
  planned_tss         what the plan asked for on those days
  tss_adherence_pct   the second against the third, never the first

On a week the plan covers fully the two ride totals are the same number and
nothing changes, which is why this went unnoticed for so long.
"""
from __future__ import annotations

import json
import plan_seam  # noqa: E402  (the plan dir of the active profile)
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module                              # noqa: E402
import clock                                          # noqa: E402
from fastapi.testclient import TestClient             # noqa: E402

SUNDAY = date(2026, 9, 20)
MONDAY = date(2026, 9, 14)
# What the rider rode, the week of the report.
RIDES = {0: 120, 1: 57, 2: 87, 3: 94, 4: 141, 5: 0, 6: 0}
FRIDAY_PRESCRIBED = 138


def _session(day: date, kind: str, tss: int, status: str = "pending") -> dict:
    return {"day": day.isoformat(), "day_name": day.strftime("%a"),
            "session_type": kind, "duration_min": 0 if tss == 0 else tss + 20,
            "tss_estimate": tss, "description": "x", "status": status,
            "zwo_file": "" if tss == 0 else "z2_steady_56pct_60min.zwo",
            "zwo_name": "" if tss == 0 else "Z2"}


class AdherenceOverTheOverlap(unittest.TestCase):
    def setUp(self):
        clock.freeze(SUNDAY)
        self.tmp = Path(tempfile.mkdtemp(prefix="overlap_"))
        self.plan_p = self.tmp / "current_plan.json"
        self._patch = plan_seam.patch_plan_dir(self.tmp)
        self._patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch.stop()
        clock.unfreeze()

    def _write_plan(self, first_day: date):
        """A plan whose current week starts on ``first_day`` -- the state a
        Thursday replan leaves behind."""
        sessions = []
        for d in range((SUNDAY - first_day).days + 1):
            day = first_day + timedelta(days=d)
            tss = FRIDAY_PRESCRIBED if day == date(2026, 9, 18) else 0
            kind = "z2" if tss else "rest"
            sessions.append(_session(day, kind, tss,
                                     "done_partial" if tss else "pending"))
        self.plan_p.write_text(json.dumps({
            "goal": {"type": "general"},
            "phases": [],
            "weeks": [{"week_num": 1, "start": first_day.isoformat(),
                       "end": SUNDAY.isoformat(), "phase": "continuous",
                       "tss_target": 204, "is_stepback": False,
                       "sessions": sessions}],
            "generated": first_day.isoformat(),
        }), encoding="utf-8")

    def _summary(self):
        rides = [{"date": (MONDAY + timedelta(days=d)).isoformat(), "tss": tss,
                  "duration_min": 60, "sport": "Ride", "avg_hr": 140,
                  "name": "ride"}
                 for d, tss in RIDES.items() if tss]
        with patch.object(app_module, "api_activities", return_value=rides):
            r = self.client.get("/api/week-summary")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_the_week_total_still_counts_the_whole_week(self):
        """The rider rode 499 TSS. That does not stop being true because the
        plan starts on Friday."""
        self._write_plan(date(2026, 9, 18))
        self.assertEqual(self._summary()["tss_done"], sum(RIDES.values()))

    def test_the_grade_is_taken_over_the_days_the_plan_covers(self):
        self._write_plan(date(2026, 9, 18))
        got = self._summary()
        self.assertEqual(got["tss_done_in_plan"], RIDES[4],
                         "Friday's ride is the only one the plan covers")
        self.assertEqual(got["planned_tss"], FRIDAY_PRESCRIBED)
        self.assertEqual(got["tss_adherence_pct"],
                         round(RIDES[4] / FRIDAY_PRESCRIBED * 100),
                         f"{got['tss_done']} over the week against "
                         f"{got['planned_tss']} over three days is the "
                         f"499/138 the rider reported")

    def test_the_card_says_how_much_of_the_week_the_plan_covers(self):
        """A grade over three days has to say so, or it reads as a week."""
        self._write_plan(date(2026, 9, 18))
        got = self._summary()
        self.assertEqual(got["plan_covers_days"], 3)
        self.assertEqual(got["plan_covers_from"], "2026-09-18")
        self.assertEqual(got["plan_covers_to"], "2026-09-20")

    def test_a_full_week_grades_exactly_as_before(self):
        """The common case must not move: when the plan covers Monday to
        Sunday, the two ride totals are one number."""
        self._write_plan(MONDAY)
        got = self._summary()
        self.assertEqual(got["tss_done_in_plan"], got["tss_done"])
        self.assertEqual(got["plan_covers_days"], 7)
        self.assertEqual(got["tss_adherence_pct"],
                         min(150, round(sum(RIDES.values()) / FRIDAY_PRESCRIBED * 100)))


if __name__ == "__main__":
    unittest.main()
