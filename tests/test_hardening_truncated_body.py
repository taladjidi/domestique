"""A truncated body is a network error, never an authentication failure
(contract 1.4).

The audit (2026-09-23, integ #9) fed training._get a 200 whose body stopped
short. `resp.read()` raised http.client.IncompleteRead, which is neither an
OSError nor a URLError, so it escaped _get untyped and unretried. The sync
loop then asked db._is_auth_error, whose last resort is a substring match:
"IncompleteRead(4031 bytes read, 12 more expected)" contains "403", so a
dropped connection counted towards disabling the rider's sync.
"""
from __future__ import annotations

import http.client

import pytest

import config
import db
import training as tr


class _Resp:
    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        raise self.exc


@pytest.fixture()
def wired(monkeypatch):
    monkeypatch.setattr(config, "ICU_ATHLETE_ID", "i1", raising=False)
    monkeypatch.setattr(config, "ICU_API_KEY", "k", raising=False)
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    calls = []

    def serve(exc):
        def fake(req, *a, **k):
            calls.append(req.full_url)
            return _Resp(exc)
        monkeypatch.setattr(tr.urllib.request, "urlopen", fake)
        return calls
    return serve


@pytest.mark.parametrize("exc", [
    http.client.IncompleteRead(b"x" * 4031, 12),
    http.client.BadStatusLine("garbage"),
    http.client.RemoteDisconnected("closed"),
], ids=["IncompleteRead", "BadStatusLine", "RemoteDisconnected"])
def test_a_cut_body_is_retried_and_raised_as_a_network_error(wired, exc):
    calls = wired(exc)
    with pytest.raises(tr.ICUNetworkError) as ei:
        tr.fetch_activities(days=7)
    assert len(calls) == 3, "a network error gets the same three attempts"
    assert db._is_auth_error(ei.value) is False


def test_an_untyped_truncated_read_is_not_an_auth_error():
    """Whatever path lets one escape untyped, "4031 bytes" is not a 403."""
    assert db._is_auth_error(http.client.IncompleteRead(b"x" * 4031, 12)) is False


def test_control_a_real_403_is_still_an_auth_error():
    assert db._is_auth_error(tr.ICUAuthError("HTTP 403 on x", status=403)) is True
