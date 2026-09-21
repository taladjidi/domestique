"""The daily adaptation is told the rider's form.

`training_planner.daily_adapt_plan` takes `tsb` and, when form is below -30,
projects a de-load: the hard days ahead drop a level of intensity. The only
production caller, `/api/plan/daily-adapt`, never passed the argument. It
defaulted to None, so the branch was dead on the one path that runs it, while
every card on the same page showed the rider's TSB.

Found by asking the app the same question through every door: the adaptation
answered "blank" for a number three other endpoints answered as -9.
"""
from __future__ import annotations

import json
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

TODAY = date(2026, 9, 16)          # a Wednesday
MONDAY = TODAY - timedelta(days=TODAY.weekday())
WRECKED = {"ctl": 40.0, "atl": 78.0, "tsb": -38.0, "source": "icu", "as_of": None}
FRESH = {"ctl": 40.0, "atl": 30.0, "tsb": 10.0, "source": "icu", "as_of": None}


def _session(day: date, kind: str, tss: int) -> dict:
    return {"day": day.isoformat(), "day_name": day.strftime("%a"),
            "session_type": kind, "duration_min": 60 if tss else 0,
            "tss_estimate": tss, "description": "x", "status": "pending",
            "zwo_file": "vo2max_6x30s-30s_120pct_44min.zwo" if tss else "",
            "zwo_name": kind}


class DailyAdaptKnowsTheForm(unittest.TestCase):
    def setUp(self):
        clock.freeze(TODAY)
        self.tmp = Path(tempfile.mkdtemp(prefix="adapt_"))
        (self.tmp / "current_plan.json").write_text(json.dumps({
            "goal": {"type": "general"}, "phases": [],
            "weeks": [{"week_num": 1, "start": MONDAY.isoformat(),
                       "end": (MONDAY + timedelta(days=6)).isoformat(),
                       "phase": "build", "tss_target": 400, "is_stepback": False,
                       "sessions": [_session(MONDAY + timedelta(days=i),
                                             "vo2max" if i in (3, 5) else "z2",
                                             110 if i in (3, 5) else 60)
                                    for i in range(7)]}],
        }), encoding="utf-8")
        self._patch = patch.object(app_module, "_plan_dir", return_value=self.tmp)
        self._patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch.stop()
        clock.unfreeze()

    def _adapt(self, state):
        with patch.object(app_module, "_fitness_state", return_value=state), \
             patch.object(app_module, "_collect_week_activities", return_value=[]):
            r = self.client.get("/api/plan/daily-adapt")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_a_wrecked_rider_gets_the_hard_days_eased(self):
        info = self._adapt(WRECKED)
        self.assertEqual(info.get("tsb"), -38.0,
                         "the adaptation was not told the rider's form")
        projected = json.dumps(info)
        self.assertIn("vo2max", projected,
                      "no hard day was considered at all; the fixture is wrong")
        self.assertTrue(
            info.get("tsb_deload_projected") or info.get("changes"),
            f"form -38 projected no de-load: {projected[:400]}")

    def test_a_fresh_rider_is_left_alone(self):
        """The guard on the guard: an adaptation that always eases is as
        useless as one that never does."""
        info = self._adapt(FRESH)
        self.assertEqual(info.get("tsb"), 10.0)
        self.assertFalse(info.get("tsb_deload_projected"),
                         "a rested rider had their hard days eased")
