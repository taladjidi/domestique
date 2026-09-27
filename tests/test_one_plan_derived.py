"""Contract 10.4 / 10.5 (Raw plan): no derived plan is persisted, and the
Plan tab's markdown is the stored plan rendered now.

At 433c6067 every generate wrote plan_<date>.md beside the plan (the sweep's
'none' writes), /api/plan served the newest such file as "Raw plan", and a
move-session afterwards left it showing the old schedule (the audit's D2);
the planner CLI wrote the file too and could push its own plan to the
intervals.icu calendar (--push-icu).
"""
from __future__ import annotations

from datetime import date, timedelta
import plan_seam  # noqa: E402  (the plan dir of the active profile)

import pytest
from fastapi.testclient import TestClient

import app as app_module
import clock
import plan_store
import training_planner as tp

GEN = {"goal": "general", "hours_per_week": 7.0, "plan_weeks": 4, "rest_days": [0]}


@pytest.fixture
def client(tmp_path, monkeypatch, freeze_clock):
    freeze_clock(date(2026, 9, 23))
    plan_seam.bind_active(tmp_path)
    plan_seam.bind_active(tmp_path)
    monkeypatch.setattr(app_module, "get_today_metrics", lambda *a, **k: {})
    c = TestClient(app_module.app)
    assert c.post("/api/plan/generate", json=GEN).status_code == 200
    return c, tmp_path


def test_generate_writes_the_plan_and_nothing_beside_it(client):
    c, d = client
    assert sorted(p.name for p in d.iterdir()
                  if not p.name.startswith("current_plan")) == []


def test_the_raw_plan_follows_the_stored_plan(client):
    c, d = client
    plan = c.get("/api/plan").json()["plan_json"]
    pend = [s for w in plan["weeks"] for s in w["sessions"]
            if s["day"] > clock.today().isoformat() and s["session_type"] != "rest"]
    src = pend[0]["day"]
    dst = next(s["day"] for w in plan["weeks"] for s in w["sessions"]
               if s["day"] > src and s["session_type"] == "rest")
    assert c.post("/api/plan/move-session", json={"date": src, "new_date": dst}).status_code == 200
    md = c.get("/api/plan").json()["plan"]
    stored = plan_store.read(app_module._pid())
    moved = next(s for w in stored["weeks"] for s in w["sessions"] if s["day"] == dst)
    dd = date.fromisoformat(dst).strftime("%d/%m")
    line = next(l for l in md.splitlines() if f" {dd} |" in l)
    assert f"| {moved['session_type']} |" in line, line
    assert md == tp.render_plan_md(stored)


def test_the_cli_writes_no_plan_and_pushes_nothing():
    src = open(tp.__file__, encoding="utf-8").read()
    assert "push_to_icu" not in src and "--push-icu" not in src
    assert "export_plan_md" not in src
