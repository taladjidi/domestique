"""The stream and FIT fetchers say WHY they have nothing (contract 1.2, fetch
side).

The audit (2026-09-23, data #4 / integ #4) found any failure in DFA
enrichment stamped a ride `icu_deleted`, which is sticky: a Wi-Fi blip, a 503
or a 429 during sync and the ride's DFA was never computed again. The cause
is at the source: fetch_activity_streams and fetch_activity_fit_file answered
None for "intervals.icu says this activity does not exist" and for "we could
not reach intervals.icu" alike, so no caller could tell them apart.

Each now has a *_result twin returning training.Fetched(status, data):
  ok        -- data is the payload
  not_found -- HTTP 404 / 410: the activity is gone upstream
  no_data   -- another 4xx (422: a Strava-origin ride ICU will not re-share)
  failed    -- network, timeout, truncated body, 429, 5xx, auth: retry later
The old functions keep their data-or-None contract for their other callers.
"""
from __future__ import annotations

import http.client
import io
import json
import urllib.error

import pytest

import config
import training as tr


class _Resp:
    def __init__(self, body=b"", exc=None, code=200):
        self.body, self.exc, self.code = body, exc, code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        if self.exc:
            raise self.exc
        return self.body

    def getcode(self):
        return self.code


def _http(code):
    def raise_(req, *a, **k):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b""))
    return raise_


def _raise(exc):
    def raise_(req, *a, **k):
        raise exc
    return raise_


def _serve(resp):
    return lambda req, *a, **k: resp


def _drop_icu_creds():
    for k in ("ICU_ATHLETE_ID", "ICU_API_KEY"):
        try:
            delattr(config, k)
        except AttributeError:
            pass


@pytest.fixture(autouse=True)
def _creds(monkeypatch, request):
    # Not monkeypatch.setattr: config resolves ICU_* through a module
    # __getattr__, so monkeypatch would record the RESOLVED value and restore
    # it as a real attribute, shadowing the resolver for every later test
    # (it made test_issue11's run_sync skip for want of an athlete id).
    config.ICU_ATHLETE_ID, config.ICU_API_KEY = "i1", "k"
    request.addfinalizer(_drop_icu_creds)
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)


CASES = [
    ("404", _http(404), "not_found"),
    ("410", _http(410), "not_found"),
    ("422", _http(422), "no_data"),
    ("401", _http(401), "failed"),   # a dead credential is not a deleted ride
    ("403", _http(403), "failed"),   # nor a Cloudflare UA block
    ("408", _http(408), "failed"),   # Request Timeout is a timeout
    ("429", _http(429), "failed"),
    ("500", _http(500), "failed"),
    ("503", _http(503), "failed"),
    ("url-error", _raise(urllib.error.URLError("Temporary failure in name resolution")), "failed"),
    ("timeout", _raise(TimeoutError("timed out")), "failed"),
    ("truncated", _serve(_Resp(exc=http.client.IncompleteRead(b"x" * 10, 5))), "failed"),
]


@pytest.mark.parametrize("fetch", ["streams", "fit"])
@pytest.mark.parametrize("label,urlopen,expected", CASES, ids=[c[0] for c in CASES])
def test_the_fetch_says_why_it_has_nothing(monkeypatch, fetch, label, urlopen, expected):
    monkeypatch.setattr(tr.urllib.request, "urlopen", urlopen)
    fn = tr.fetch_activity_streams_result if fetch == "streams" else tr.fetch_activity_fit_file_result
    r = fn("i4242")
    assert r.status == expected, (label, r)
    assert r.data is None
    # the legacy data-or-None contract is unchanged for the other callers
    legacy = tr.fetch_activity_streams if fetch == "streams" else tr.fetch_activity_fit_file
    assert legacy("i4242") is None


def test_streams_ok(monkeypatch):
    body = json.dumps([{"type": "hrv", "data": [800, 810]},
                       {"type": "watts", "data": [200, 210]}]).encode()
    monkeypatch.setattr(tr.urllib.request, "urlopen", _serve(_Resp(body)))
    r = tr.fetch_activity_streams_result("i4242")
    assert r.status == "ok"
    assert r.data == {"hrv": [800, 810], "watts": [200, 210]}
    assert tr.fetch_activity_streams("i4242") == r.data


def test_fit_ok(monkeypatch):
    monkeypatch.setattr(tr.urllib.request, "urlopen", _serve(_Resp(b"\x0e\x10FIT")))
    r = tr.fetch_activity_fit_file_result("i4242")
    assert r == tr.Fetched("ok", b"\x0e\x10FIT")
    assert tr.fetch_activity_fit_file("i4242") == b"\x0e\x10FIT"


def test_no_credentials_is_a_failure_to_ask_not_a_deletion(monkeypatch):
    monkeypatch.setattr(tr, "_require_credentials",
                        lambda: (_ for _ in ()).throw(tr.ICUCredentialsMissing("x")))
    assert tr.fetch_activity_streams_result("i1").status == "failed"
    assert tr.fetch_activity_fit_file_result("i1").status == "failed"


def test_server_error_carries_its_http_status(monkeypatch):
    monkeypatch.setattr(tr.urllib.request, "urlopen", _http(404))
    with pytest.raises(tr.ICUServerError) as ei:
        tr._get("activity/i1/streams")
    assert ei.value.status == 404


# ── the end-to-end rule, which app.py applies in Wave 2 ──────────────────────
# app._augment_icu_record_with_dfa still reads the legacy None and stamps
# `icu_deleted` on any failure. These are the audit's reproduction (rv-integ
# p3), strict-xfail until Wave 2 moves it onto the *_result fetchers: an
# XPASS then fails the run, so whoever lands the fix must drop the marker.

import datetime  # noqa: E402
from unittest import mock  # noqa: E402

import app as app_module  # noqa: E402
import db  # noqa: E402
import ride_storage as rs  # noqa: E402
from profile_manager import ProfileManager  # noqa: E402

ACT = {"id": "i4242", "type": "Ride",
       "start_date_local": f"{datetime.date.today()}T08:00:00",
       "moving_time": 3600, "icu_training_load": 60, "average_watts": 200,
       "distance": 30000}


@pytest.fixture
def profile(tmp_path):
    p = mock.patch("pathlib.Path.home", return_value=tmp_path)
    p.start()
    old = ProfileManager._instance
    ProfileManager._instance = None
    old_db = db.DB_PATH
    pm = ProfileManager.get()
    pm.switch(pm.create_profile("Alpha"))
    db._sync_stop.clear()
    yield pm
    p.stop()
    ProfileManager._instance = old
    db.set_db_path(old_db)


def _status_after(monkeypatch, urlopen):
    path = rs.persist_icu_activity(dict(ACT))
    monkeypatch.setattr(tr.urllib.request, "urlopen", urlopen)
    app_module._augment_icu_record_with_dfa(path, "i4242")
    return json.loads(path.read_text())["dfa_alpha1_status"]


WAVE2 = pytest.mark.xfail(strict=True, reason=(
    "Wave 2: app._augment_icu_record_with_dfa must mark icu_deleted only when "
    "both fetch_*_result answers are not_found (notes/hardening-interfaces-storage.md)"))


@WAVE2
def test_network_down_does_not_mark_the_ride_deleted(profile, monkeypatch):
    st = _status_after(monkeypatch, _raise(urllib.error.URLError("no DNS")))
    assert st != "icu_deleted"


@WAVE2
def test_http_503_does_not_mark_the_ride_deleted(profile, monkeypatch):
    st = _status_after(monkeypatch, _http(503))
    assert st != "icu_deleted"


def test_control_404_marks_the_ride_deleted(profile, monkeypatch):
    assert _status_after(monkeypatch, _http(404)) == "icu_deleted"
