"""/api/plan/daily-adapt projects what the sync's adaptation will write.

It ran `training_planner.daily_adapt_plan`, a second algorithm (first
without the rider's form -- found by asking the app the same question
through every door -- then with it), which the plan never ran: the
projection promised changes the sync would not make (the single-source
audit's D9). Contract 10.5: it is a dry run of the real on-sync adaptation,
`_apply_plan_update`, on a copy of the stored plan. So the test is equality:
the projection, then the sync's write, the same changes.
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
        self._patch = plan_seam.patch_plan_dir(self.tmp)
        self._patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch.stop()
        clock.unfreeze()

    def _inputs(self, state):
        return [patch.object(app_module, "get_today_metrics", return_value=state),
                patch.object(app_module, "_fitness_state", return_value=state),
                patch.object(app_module, "_load_all_rides_safe", return_value=[]),
                patch("db.query_activities", return_value=[])]

    def _projection_then_write(self, state):
        import contextlib
        import plan_store
        with contextlib.ExitStack() as st:
            for p in self._inputs(state):
                st.enter_context(p)
            r = self.client.get("/api/plan/daily-adapt")
            self.assertEqual(r.status_code, 200, r.text)
            info = r.json()
            pid = app_module._pid()
            before = {x["day"]: x for w in plan_store.read(pid)["weeks"] for x in w["sessions"]}
            app_module._maybe_auto_reforecast(pid, 1)
            after = {x["day"]: x for w in plan_store.read(pid)["weeks"] for x in w["sessions"]}
        keys = ("session_type", "duration_min", "tss_estimate")
        wrote = sorted(d for d in after if d >= TODAY.isoformat()
                       and tuple(after[d].get(k) for k in keys) != tuple(before[d].get(k) for k in keys))
        return info, wrote

    def test_a_wrecked_rider_is_projected_what_the_sync_writes(self):
        info, wrote = self._projection_then_write(WRECKED)
        self.assertTrue(info["projection_only"])
        self.assertEqual(sorted(c["date"] for c in info["projected_adaptations"]), wrote)
        self.assertTrue(wrote, "the sync changed nothing: the equality would be vacuous")

    def test_a_fresh_rider_is_projected_what_the_sync_writes(self):
        info, wrote = self._projection_then_write(FRESH)
        self.assertEqual(sorted(c["date"] for c in info["projected_adaptations"]), wrote)
