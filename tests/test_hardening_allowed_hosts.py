"""Contract 7.2: a request whose Host is not an allowed host gets 400.

No Host check meant DNS rebinding: a page on rebind.attacker.example whose
name later resolves to 127.0.0.1 is same-origin with the app in the victim's
browser and reads every GET (settings, /api/logs, diag) past the nginx login
(audit 2026-09-23, rv-http #3; originprobe.py: GET Host=attacker -> 200).

Allowed: 127.0.0.1, localhost, [::1] on any port, plus the comma-separated
DOMESTIQUE_ALLOWED_HOSTS (production nginx forwards
Host: domestique.aladjidi.xyz). The suite's TestClient host "testserver" is
allowed by tests/conftest.py through that same variable, not by the app.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module

c = TestClient(app_module.app, raise_server_exceptions=False)


@pytest.mark.parametrize("host", [
    "rebind.attacker.example:22400", "rebind.attacker.example", "evil.com",
    "127.0.0.1.nip.io", "localhost.evil.com", "domestique.aladjidi.xyz",
    "[::2]:22400", "0.0.0.0:22400", "",
])
@pytest.mark.parametrize("method,path", [("GET", "/api/settings"),
                                          ("GET", "/api/logs"),
                                          ("POST", "/api/retest-nudge/snooze"),
                                          ("GET", "/")])
def test_a_foreign_host_is_400(monkeypatch, host, method, path):
    monkeypatch.setenv("DOMESTIQUE_ALLOWED_HOSTS", "testserver")
    r = c.request(method, path, headers={"Host": host}, json={} if method == "POST" else None)
    assert r.status_code == 400, (host, r.status_code)


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.1:22400", "localhost", "localhost:8080",
    "LOCALHOST:22400", "[::1]", "[::1]:22400",
])
def test_loopback_hosts_on_any_port_are_allowed(monkeypatch, host):
    monkeypatch.delenv("DOMESTIQUE_ALLOWED_HOSTS", raising=False)
    r = c.get("/api/version", headers={"Host": host})
    assert r.status_code == 200, (host, r.status_code)


def test_the_env_var_allows_the_proxied_vhost(monkeypatch):
    monkeypatch.setenv("DOMESTIQUE_ALLOWED_HOSTS", " domestique.aladjidi.xyz , other.example")
    for host in ("domestique.aladjidi.xyz", "DOMESTIQUE.aladjidi.xyz:443", "other.example"):
        assert c.get("/api/version", headers={"Host": host}).status_code == 200, host
    assert c.get("/api/version", headers={"Host": "x.domestique.aladjidi.xyz"}).status_code == 400
    monkeypatch.delenv("DOMESTIQUE_ALLOWED_HOSTS")
    assert c.get("/api/version", headers={"Host": "domestique.aladjidi.xyz"}).status_code == 400


def test_testserver_comes_from_the_test_environment_not_the_app(monkeypatch):
    monkeypatch.delenv("DOMESTIQUE_ALLOWED_HOSTS", raising=False)
    assert c.get("/api/version").status_code == 400      # TestClient's "testserver"
