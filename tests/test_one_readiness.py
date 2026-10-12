"""One readiness, unknown when unknown (contract slim/9.3, R1-R3).

With no wellness (the owner's sandbox, an intervals.icu outage, the Garmin
block) /api/today-session used to set readiness to 50 and
adjust_today_session's 40-59 rule then served every hard day as Z2; the
picker invented 70; and /api/readiness fell back to the local wellness store
while today-session did not, so the gauge and the adjustment could read
different data.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import clock
import training_planner as tp


def _planned(st="vo2max", dur=60):
    return tp.PlannedSession(day=clock.today(), day_name="Mon", session_type=st,
                             duration_min=dur, tss_estimate=70, description="")


_NO_GATES = dict(hrv_streak_below_swc=0, yesterday_tss_ratio=1.0, rides_recent=[],
                 daily_log_today={"hooper_index": 4})


class TestAdjustmentWithUnknownReadiness(unittest.TestCase):
    """R1 in the planner: a None score skips the score rules."""

    def test_hard_day_served_as_planned_when_readiness_unknown(self):
        for st in ("vo2max", "threshold", "overunder", "sweetspot", "ftp_test"):
            adj, reason = tp.adjust_today_session(planned=_planned(st), readiness={"score": None},
                                                  **_NO_GATES)
            self.assertEqual((adj.session_type, reason), (st, ""), st)

    def test_a_real_low_score_still_rules(self):
        adj, reason = tp.adjust_today_session(planned=_planned(), readiness={"score": 50},
                                              **_NO_GATES)
        self.assertEqual(adj.session_type, "z2")
        self.assertIn("Readiness 50", reason)
        adj, _ = tp.adjust_today_session(planned=_planned(), readiness={"score": 0}, **_NO_GATES)
        self.assertEqual(adj.session_type, "rest")   # a real 0 is not "unknown"

    def test_gates_with_data_still_apply_when_readiness_unknown(self):
        adj, reason = tp.adjust_today_session(
            planned=_planned(), readiness={"score": None}, hrv_streak_below_swc=0,
            yesterday_tss_ratio=1.0, rides_recent=[],
            daily_log_today={"soreness": 6, "hooper_index": 4})
        self.assertEqual(adj.session_type, "recovery")
        self.assertIn("soreness", reason)


class _Endpoints(unittest.TestCase):
    """A vo2max day in a stored plan, every wellness source empty unless a
    test gives one."""

    def setUp(self):
        app_module.clear_cache()
        self.addCleanup(app_module.clear_cache)
        self.client = TestClient(app_module.app)
        today = clock.today()
        monday = today - timedelta(days=today.weekday())
        tmp = Path(tempfile.mkdtemp(prefix="one_readiness_"))
        (tmp / "current_plan.json").write_text(json.dumps({
            "goal": {"type": "general"}, "phases": [],
            "weeks": [{"week_num": 1, "start": monday.isoformat(),
                       "end": (monday + timedelta(days=6)).isoformat(), "phase": "build",
                       "tss_target": 400, "is_stepback": False,
                       "sessions": [{"day": today.isoformat(), "day_name": today.strftime("%a"),
                                     "session_type": "vo2max", "duration_min": 75,
                                     "tss_estimate": 85, "description": "VO2max",
                                     "zwo_file": "", "zwo_name": "", "status": "pending"}]}]}))
        self.local = {}
        for p in (patch.object(app_module, "_plan_dir", return_value=tmp),
                  patch.object(app_module, "get_today_metrics", return_value={}),
                  patch.object(app_module, "get_sleep_metrics", return_value={}),
                  patch.object(app_module, "_local_sleep_metrics", side_effect=lambda days=14: dict(self.local)),
                  patch.object(app_module, "_get_soreness_subjective", return_value=None),
                  patch.object(app_module, "_recent_dfa_and_decoupling", return_value=([], None, None, None)),
                  patch.object(app_module, "_load_all_rides_safe", return_value=[]),
                  patch.object(app_module.db, "query_activities", return_value=[]),
                  patch.object(app_module.db, "get_daily_log_today", return_value={}),
                  patch.object(tp, "_hooper_index_today", return_value=0)):
            p.start()
            self.addCleanup(p.stop)

    def _both(self):
        t = self.client.get("/api/today-session").json()
        r = self.client.get("/api/readiness").json()
        return t, r


class TestNoWellnessIsUnknown(_Endpoints):

    def test_hard_day_served_as_planned_and_said_unknown(self):
        t, r = self._both()
        # R1: the vo2max day is served as planned, no reason quotes a score
        self.assertEqual(t["adjusted"]["session_type"], "vo2max")
        self.assertFalse(t["was_modified"])
        self.assertNotIn("Readiness", t.get("reason") or "")
        # R2: score null and the status says what is missing, on both
        self.assertIsNone(t["readiness"])
        self.assertIsNone(r["readiness"]["score"])
        self.assertIsNone(r["score_0_100"])
        self.assertIsNone(r["score_0_10"])
        self.assertEqual(t["readiness_detail"]["data_status"], "insufficient_data")
        self.assertEqual(r["readiness"]["data_status"], "insufficient_data")
        self.assertIn("hrv", r["readiness"]["missing"])
        # R3: same status, same missing list
        for k in ("score", "status", "data_status", "missing"):
            self.assertEqual(t["readiness_detail"][k], r["readiness"][k], k)


class TestLocalWellnessIsReadByBoth(_Endpoints):

    def test_today_session_reads_the_local_store_too(self):
        self.local = {"ln_rmssd_7d": 3.0, "swc_lower": 2.9, "swc_upper": 3.2,
                      "sleep_h": 5.5, "rhr_delta": 6.0, "red_hrv_streak": 0}
        t, r = self._both()
        self.assertIsNotNone(r["readiness"]["score"])
        self.assertEqual(r["readiness"]["data_status"], "local_wellness")
        for k in ("score", "status", "data_status", "missing"):
            self.assertEqual(t["readiness_detail"][k], r["readiness"][k], k)
        self.assertEqual(t["readiness"], r["score_0_100"])


if __name__ == "__main__":
    unittest.main()


# ── R2 on the dashboard: the Today verdict card says "readiness unknown" ──

def _extract_js_function(src: str, name: str) -> str:
    i = src.index(f"async function {name}") if f"async function {name}" in src else src.index(f"function {name}")
    depth = 0
    for j in range(src.index("{", i), len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(name)


def test_today_verdict_card_says_readiness_unknown():
    import shutil
    import subprocess
    if shutil.which("node") is None:
        raise unittest.SkipTest("node not installed")
    src = (Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html").read_text()
    harness = """
const host = { innerHTML: '' };
const document = { getElementById: () => host };
const window = {};
const esc = s => String(s);
let payload;
async function getResource() { return { ok: true, json: async () => payload }; }
""" + _extract_js_function(src, "loadReadinessComposite") + """
(async () => {
  payload = { score_0_100: null, severity: 'normal', severity_reasons: [],
              readiness: { score: null, data_status: 'insufficient_data', missing: ['hrv', 'sleep'],
                           advice: 'Ensure Garmin is synced with Intervals.icu' } };
  await loadReadinessComposite();
  if (host.innerHTML.indexOf('Readiness unknown (no hrv, sleep)') < 0) throw new Error('unknown not said: ' + host.innerHTML);
  payload = { score_0_100: 72, severity: 'normal', severity_reasons: [],
              readiness: { score: 72, advice: 'Z2 / planned moderate session' } };
  await loadReadinessComposite();
  if (host.innerHTML.indexOf('Readiness unknown') >= 0 || host.innerHTML.indexOf('72') < 0)
    throw new Error('a real score must show: ' + host.innerHTML);
})().catch(e => { console.error(e.message); process.exit(1); });
"""
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr + res.stdout
