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
