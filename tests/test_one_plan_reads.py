"""Contract 10.2: plan_store.read is the one reader, and a stored plan that
does not parse is ONE answer on every route.

The audit (d190fa82, its D5) found four answers for one corrupt
current_plan.json: /api/plan served the markdown as the plan,
/api/weekly-plan said there was no plan on record, /api/calendar put
E_PLAN_PARSE_CORRUPT in a 200, a POST generate wrote around the store. Now
every route that reads the plan answers 409 E_PLAN_PARSE_CORRUPT; generate,
the action that repairs it, replaces it (the unreadable file kept as the
newest backup); the health report describes it with the same code.
"""
from __future__ import annotations

import json
import plan_seam  # noqa: E402  (the plan dir of the active profile)
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute

import app as app_module
import clock
import plan_store
import training_planner as tp

CORRUPT = b'{"weeks": ['
D5_ROUTES = ["/api/plan", "/api/weekly-plan", "/api/calendar", "/api/today-session",
             "/api/week-summary"]


@pytest.fixture
def corrupt(tmp_path, monkeypatch):
    plan_seam.bind_active(tmp_path)
    plan_seam.bind_active(tmp_path)
    (tmp_path / "current_plan.json").write_bytes(CORRUPT)
    return tmp_path


def _answer(r):
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body.get("code") if isinstance(body, dict) else None


def test_the_audits_four_answers_are_one(corrupt):
    c = TestClient(app_module.app)
    got = {u: _answer(c.get(u)) for u in D5_ROUTES}
    assert set(got.values()) == {(409, "E_PLAN_PARSE_CORRUPT")}, got


def test_every_get_that_reads_the_plan_gives_the_one_answer(corrupt, monkeypatch):
    """Every GET route, with a spy on the one reader: a route that tried to
    read the plan answers 409 E_PLAN_PARSE_CORRUPT, whatever it does with
    the exception (a route that swallows it into a fallback is overridden)."""
    reads = []
    real = plan_store.read

    def spy(pid):
        reads.append(pid)
        return real(pid)
    monkeypatch.setattr(plan_store, "read", spy)
    c = TestClient(app_module.app, raise_server_exceptions=False)
    wrong, readers = {}, 0
    for rt in app_module.app.routes:
        if not isinstance(rt, APIRoute) or "GET" not in rt.methods or "{" in rt.path:
            continue
        if rt.path in ("/", "/api/diag/health"):
            continue
        reads.clear()
        r = c.get(rt.path)
        if reads:
            readers += 1
            if _answer(r) != (409, "E_PLAN_PARSE_CORRUPT"):
                wrong[rt.path] = _answer(r)
    assert readers >= len(D5_ROUTES), readers
    assert wrong == {}


def test_a_writer_on_a_corrupt_plan_gives_the_one_answer_and_writes_nothing(corrupt):
    c = TestClient(app_module.app, raise_server_exceptions=False)
    day = (clock.today() + timedelta(days=2)).isoformat()
    day2 = (clock.today() + timedelta(days=3)).isoformat()
    for url, body in (("/api/plan/move-session", {"date": day, "new_date": day2}),
                      ("/api/plan/reforecast", None), ("/api/plan/dismiss-session", {"date": day})):
        r = c.post(url, json=body) if body is not None else c.post(url)
        assert _answer(r) == (409, "E_PLAN_PARSE_CORRUPT"), (url, r.status_code, r.text[:200])
    assert (corrupt / "current_plan.json").read_bytes() == CORRUPT


def test_generate_replaces_a_corrupt_plan_through_the_store(corrupt):
    c = TestClient(app_module.app)
    r = c.post("/api/plan/generate", json={"goal": "general", "hours_per_week": 7.0,
                                            "plan_weeks": 4, "rest_days": [0]})
    assert r.status_code == 200, r.text[:300]
    assert isinstance(json.loads((corrupt / "current_plan.json").read_text()), dict)
    assert (corrupt / "current_plan.json.bak").read_bytes() == CORRUPT


def test_the_health_report_describes_it(corrupt, monkeypatch):
    monkeypatch.setattr(app_module, "_diag_local_only", lambda request: True)
    app_module._DIAG_HEALTH_CACHE.clear()
    r = TestClient(app_module.app).get("/api/diag/health")
    assert r.status_code == 200
    assert r.json()["checks"]["plan_readable"]["code"] == "E_PLAN_PARSE_CORRUPT"
