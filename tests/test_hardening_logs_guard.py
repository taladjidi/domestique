"""Contract 7.8: /api/logs has the same local-only guard as /api/diag/*.

The log tail carries paths, ride ids and exception text; /api/diag/* already
answered 403 to a non-local client, /api/logs answered anyone who reached it
(audit 2026-09-23, rv-http #10).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.mark.parametrize("path", ["/api/logs?lines=5", "/api/diag/health",
                                  "/api/diag/recent-errors"])
def test_a_remote_client_is_refused(path):
    remote = TestClient(app_module.app, client=("203.0.113.9", 40000))
    assert remote.get(path).status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "testclient"])
def test_a_local_client_still_reads_the_logs(host):
    local = TestClient(app_module.app, client=(host, 40000))
    r = local.get("/api/logs?lines=5")
    assert r.status_code == 200
    assert "lines" in r.json()
