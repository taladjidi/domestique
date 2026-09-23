"""Contract 7.7: the setup wizard's wellness prefill window is relative to
today.

/api/setup/test-icu asked intervals.icu for wellness between the fixed dates
2025-01-01 and 2026-12-31: from 2027 on the eFTP prefill would find nothing
(audit 2026-09-23, rv-http #9).
"""
from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs, urlparse

import app as app_module


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
        self.content = b"x"

    def json(self):
        return self._p


def test_the_prefill_asks_for_the_year_up_to_today(monkeypatch, freeze_clock):
    import httpx
    from fastapi.testclient import TestClient

    freeze_clock(date(2027, 3, 1))
    asked = []

    def fake_get(url, *a, **k):
        if url.endswith("/athlete/i7"):
            return _Resp(200, {"name": "Rider", "weight": 70})
        asked.append(url)
        q = parse_qs(urlparse(url).query)
        if q["oldest"][0] <= "2027-02-20" <= q["newest"][0]:
            return _Resp(200, [{"id": "2027-02-20",
                                "sportInfo": [{"type": "Ride", "eftp": 263.4}]}])
        return _Resp(200, [])

    monkeypatch.setattr(httpx, "get", fake_get)
    r = TestClient(app_module.app).post("/api/setup/test-icu",
                                        json={"athlete_id": "i7", "api_key": "k"})
    assert r.status_code == 200
    assert r.json()["eftp"] == 263
    (url,) = asked
    q = parse_qs(urlparse(url).query)
    assert q["newest"] == ["2027-03-01"]
    assert q["oldest"] == ["2026-03-01"]
