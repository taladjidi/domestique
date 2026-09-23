"""Contract 7.4: no GET mutates state.

- `GET /api/profile/pr-toast-queue?drain=1` cleared the queue: a GET from any
  page (or a prefetch) consumed the rider's PR toasts (audit 2026-09-23,
  rv-http #6; drainprobe.py). The drain is a POST.
- GET /api/activities, /api/today-session and /api/calendar each started the
  lazy intervals.icu sync thread. It is started by one explicit POST,
  /api/icu/lazy-sync, which the dashboard calls on load.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app as app_module

DASHBOARD = Path(app_module.__file__).parent / "templates" / "dashboard.html"


@pytest.fixture()
def client():
    return TestClient(app_module.app, raise_server_exceptions=False)


def test_a_get_does_not_drain_the_pr_toasts(client):
    entry = {"ride_id": "icu_i1", "n_prs": 2}
    app_module._write_pr_toast_queue([entry])
    try:
        for _ in range(2):
            r = client.get("/api/profile/pr-toast-queue?drain=1")
            assert r.status_code == 200
            assert r.json()["queue"] == [entry]
        assert app_module._read_pr_toast_queue() == [entry]

        r = client.post("/api/profile/pr-toast-queue/drain")
        assert r.status_code == 200
        assert r.json()["queue"] == [entry]
        assert app_module._read_pr_toast_queue() == []
    finally:
        app_module._write_pr_toast_queue([])


@pytest.mark.parametrize("path", ["/api/activities", "/api/today-session", "/api/calendar"])
def test_a_get_does_not_start_the_icu_sync(client, path):
    started = []
    with patch.object(app_module, "_icu_credentials_present", return_value=True), \
         patch.object(app_module, "_read_last_sync_at", return_value=None), \
         patch.object(app_module, "_maybe_lazy_icu_sync",
                      side_effect=lambda **k: started.append(k)), \
         patch.object(app_module, "get_today_metrics", lambda: {}):
        client.get(path)
        for t in threading.enumerate():
            if t.name == "domestique.icu_sync":
                t.join(5)
    assert started == []
    assert not app_module._icu_sync_in_progress.is_set()


def test_the_explicit_post_starts_it(client):
    started = threading.Event()
    with patch.object(app_module, "_maybe_lazy_icu_sync",
                      side_effect=lambda **k: started.set()):
        r = client.post("/api/icu/lazy-sync")
        assert r.status_code == 200
        assert r.json()["started"] is True
        assert started.wait(5)
    assert client.get("/api/icu/lazy-sync").status_code == 405


def test_the_dashboard_calls_the_posts():
    html = DASHBOARD.read_text(encoding="utf-8")
    assert "pr-toast-queue?drain" not in html
    for route in ("/api/profile/pr-toast-queue/drain", "/api/icu/lazy-sync"):
        calls = re.findall(r"fetch\('%s'(,\s*\{[^}]*\})?\)" % re.escape(route), html)
        assert calls, f"the dashboard never fetches {route}"
        assert all("method: 'POST'" in opts or 'method:"POST"' in opts for opts in calls), calls
