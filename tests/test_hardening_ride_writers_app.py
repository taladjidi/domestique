"""Contracts 1.2 and 2.2, the app / power_curve side.

1.2 -- DFA enrichment marks a ride `icu_deleted` (sticky: never retried)
only when intervals.icu confirmed both the streams and the FIT are gone
(404/410). A network error, 429 or 5xx is `fetch_failed`, which the next
sync pass retries; after recovery the ride is computed (audit 2026-09-23,
data #4 / integ #4).

2.2 -- every ride-record writer in app.py and power_curve.py merges through
ride_storage.update_ride / update_json_record (per-record lock, fresh read,
atomic replace). The DFA write-back wrote the WHOLE record it had read
before up to 45 s of fetches; the power-curve backfill did the same across
its stream fetch; the lazy PR persist across its compute; an RPE or an
FTP-test verdict saved meanwhile was erased (data #2).
"""
from __future__ import annotations

import datetime
import json
import re
import threading
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import app as app_module
import db
import power_curve
import ride_storage as rs
import training as tr
from profile_manager import ProfileManager

EXT = "i5150"
ACT = {"id": EXT, "type": "Ride", "name": "Morning",
       "start_date_local": f"{datetime.date.today()}T08:00:00",
       "moving_time": 3600, "elapsed_time": 3700, "icu_training_load": 60,
       "average_watts": 200, "distance": 30000, "source": "GARMIN_CONNECT"}
COMPUTED = {"dfa_alpha1_status": "computed", "dfa_alpha1_avg": 0.81,
            "dfa_alpha1_series": [0.8, 0.82], "dfa_alpha1_lt1_minutes": 12.0,
            "rr_intervals_count": 4000}


@pytest.fixture
def ride(tmp_path):
    p = mock.patch("pathlib.Path.home", return_value=tmp_path)
    p.start()
    old = ProfileManager._instance
    ProfileManager._instance = None
    old_db = db.DB_PATH
    pm = ProfileManager.get()
    pm.switch(pm.create_profile("Alpha"))
    db._sync_stop.clear()
    path = rs.persist_icu_activity(dict(ACT))
    yield path
    p.stop()
    ProfileManager._instance = old
    db.set_db_path(old_db)


def _fetchers(monkeypatch, streams, fit):
    monkeypatch.setattr(tr, "fetch_activity_streams_result", lambda _id: streams)
    monkeypatch.setattr(tr, "fetch_activity_fit_file_result", lambda _id: fit)


def _status(path):
    return json.loads(Path(path).read_text())["dfa_alpha1_status"]


# ── 1.2 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("streams,fit,want", [
    ("failed", "failed", "fetch_failed"),       # network down, 429, 5xx
    ("failed", "not_found", "fetch_failed"),    # one side never answered
    ("not_found", "failed", "fetch_failed"),
    ("not_found", "not_found", "icu_deleted"),  # confirmed gone, both ways
    ("no_data", "no_data", "no_rr_data"),       # exists, nothing to compute from
    ("not_found", "no_data", "no_rr_data"),
])
def test_the_status_follows_what_intervals_icu_said(ride, monkeypatch, streams, fit, want):
    _fetchers(monkeypatch, tr.Fetched(streams), tr.Fetched(fit))
    app_module._augment_icu_record_with_dfa(ride, EXT)
    assert _status(ride) == want


def test_a_failed_fetch_is_retried_and_computed_after_recovery(ride, monkeypatch):
    import analytics
    _fetchers(monkeypatch, tr.Fetched("failed"), tr.Fetched("failed"))
    app_module._augment_icu_record_with_dfa(ride, EXT)
    assert _status(ride) == "fetch_failed"
    monkeypatch.setattr(analytics, "compute_dfa_alpha1_from_hrv_stream",
                        lambda hrv: dict(COMPUTED))
    _fetchers(monkeypatch, tr.Fetched("ok", {"hrv": [800] * 10}), tr.Fetched("failed"))
    app_module._augment_icu_record_with_dfa(ride, EXT)       # the next pass
    rec = json.loads(ride.read_text())
    assert rec["dfa_alpha1_status"] == "computed"
    assert rec["dfa_alpha1_avg"] == 0.81


# ── 2.2 ─────────────────────────────────────────────────────────────────────

def _pause_inside(monkeypatch, obj, name, result):
    paused, release = threading.Event(), threading.Event()

    def slow(*a, **k):
        paused.set()
        release.wait(20)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(obj, name, slow)
    return paused, release


def _while_paused(paused, release, work, rider_writes):
    t = threading.Thread(target=work)
    t.start()
    assert paused.wait(20)
    rider_writes()
    release.set()
    t.join(30)
    assert not t.is_alive()


def _rate_and_review(client):
    assert client.post(f"/api/ride/icu_{EXT}/rpe", json={"rpe": 7}).status_code == 200
    r = client.post(f"/api/ride/icu_{EXT}/ftp-test-review",
                    json={"action": "declined", "ftp": 250})
    assert r.status_code == 200 and r.json()["persisted"] is True


def _assert_rider_input_kept(path):
    rec = json.loads(Path(path).read_text())
    assert rec.get("rpe") == 7, "the write-back erased the rider's RPE"
    assert (rec.get("ftp_test_review") or {}).get("action") == "declined"
    return rec


def test_rider_input_saved_during_the_dfa_fetch_survives_its_write_back(ride, monkeypatch):
    import analytics
    monkeypatch.setattr(analytics, "compute_dfa_alpha1_from_hrv_stream",
                        lambda hrv: dict(COMPUTED))
    paused, release = _pause_inside(monkeypatch, tr, "fetch_activity_streams_result",
                                    tr.Fetched("ok", {"hrv": [800] * 10}))
    c = TestClient(app_module.app)
    _while_paused(paused, release,
                  lambda: app_module._augment_icu_record_with_dfa(ride, EXT),
                  lambda: _rate_and_review(c))
    rec = _assert_rider_input_kept(ride)
    assert rec["dfa_alpha1_status"] == "computed"


def test_rider_input_saved_during_a_power_curve_backfill_survives_it(ride, monkeypatch):
    n = 700
    paused, release = _pause_inside(monkeypatch, tr, "fetch_activity_streams_result",
                                    tr.Fetched("ok", {"watts": [200] * n, "heartrate": [140] * n}))
    c = TestClient(app_module.app)
    _while_paused(paused, release,
                  lambda: power_curve.backfill_icu_history("default", max_per_second=100),
                  lambda: _rate_and_review(c))
    rec = _assert_rider_input_kept(ride)
    assert rec.get("efforts"), "the backfill's own merge was lost"


@pytest.mark.parametrize("gated", [False, True])
def test_rider_input_saved_during_a_failed_backfill_fetch_survives_the_marker(ride, monkeypatch, gated):
    """Both write paths of _mark_no_streams (with and without the sync
    gate) merge the marker into the record as it is now. The gated one
    was untested: writing its stale copy there failed nothing (fix-forward
    F2 evaluator)."""
    paused, release = _pause_inside(monkeypatch, tr, "fetch_activity_streams_result",
                                    tr.Fetched("not_found"))
    c = TestClient(app_module.app)
    snap = db.snapshot_sync_identity() if gated else None
    _while_paused(paused, release,
                  lambda: power_curve.backfill_icu_history(
                      "default", max_per_second=100, sync_snapshot=snap),
                  lambda: _rate_and_review(c))
    rec = _assert_rider_input_kept(ride)
    assert rec.get("no_streams_available") is True


@pytest.mark.parametrize("status,terminal", [
    ("not_found", True),    # 404/410: intervals.icu says it is gone
    ("no_data", True),      # 422 & co: it exists, we cannot have its streams
    ("failed", False),      # network, timeout, 429, 5xx: ask again next pass
])
def test_only_an_answer_marks_a_ride_as_having_no_streams(ride, monkeypatch, status, terminal):
    monkeypatch.setattr(tr, "fetch_activity_streams_result", lambda _id: tr.Fetched(status))
    r = power_curve.backfill_icu_history("default", max_per_second=100)
    rec = json.loads(ride.read_text())
    assert (rec.get("no_streams_available") is True) is terminal, rec
    assert r["failed"] == 1


def test_a_network_blip_does_not_blank_the_power_curve_forever(ride, monkeypatch):
    """Fix-forward F2 (the evaluator's p_pc_terminal): the backfill used the
    legacy fetch_activity_streams (None on ANY failure) and stamped the
    terminal no_streams_available on a network error, so the ride never
    got a power curve. A failed fetch is now retried by the next pass."""
    monkeypatch.setattr(tr, "_require_credentials", lambda: None)
    n = 700

    def down(path, params=None):
        raise tr.ICUNetworkError("URL error: Network is unreachable")

    def up(path, params=None):
        return [{"type": "watts", "data": [200 + i % 50 for i in range(n)]},
                {"type": "time", "data": list(range(n))}]
    monkeypatch.setattr(tr, "_get", down)
    power_curve.backfill_icu_history("default", max_per_second=100)
    rec = json.loads(ride.read_text())
    assert rec.get("no_streams_available") is not True, "a network blip was marked terminal"
    rs.update_json_record(ride, lambda r: {**r, "streams_retry_after": 0})   # cooldown over
    monkeypatch.setattr(tr, "_get", up)
    power_curve.backfill_icu_history("default", max_per_second=100)
    rec = json.loads(ride.read_text())
    assert rec.get("efforts") and (rec.get("streams") or {}).get("watts")


def test_rider_input_saved_during_the_lazy_pr_compute_survives_its_persist(ride, monkeypatch):
    rs.update_ride(EXT, lambda r: {k: v for k, v in r.items() if k != "prs"})
    paused, release = _pause_inside(monkeypatch, power_curve, "compute_ride_prs", [{"d": 5}])
    c = TestClient(app_module.app)
    _while_paused(paused, release,
                  lambda: c.get(f"/api/ride/icu_{EXT}/prs"),
                  lambda: _rate_and_review(c))
    rec = _assert_rider_input_kept(ride)
    assert rec["prs"] == [{"d": 5}]


def test_no_ride_writer_writes_the_file_itself():
    app_src = (Path(app_module.__file__)).read_text(encoding="utf-8")
    pc_src = Path(power_curve.__file__).read_text(encoding="utf-8")
    assert not re.search(r'_icu_rides_dir\(\)\s*/\s*f"\{ext\}\.json"', app_src)
    assert "rec_path.write_text" not in app_src
    assert "_atomic_write_json(ride_path" not in pc_src


def test_a_failed_fetch_waits_before_it_is_asked_again(ride, monkeypatch):
    """Review of the F2 fix: `failed` is retryable, but nothing spaced the
    retries -- during an outage (or with a revoked key) every power-curve
    GET relaunched a pass that asked for every ride's streams again
    (reviewer's p_relaunch: 20 rides x 10 GETs = 200 asks, progress stuck
    at 0 %). A failed ride now waits out a cooldown, counts as not-missing
    meanwhile, and is asked again after it."""
    asks = []
    monkeypatch.setattr(tr, "fetch_activity_streams_result",
                        lambda _id: (asks.append(_id), tr.Fetched("failed"))[1])
    power_curve.backfill_icu_history("default", max_per_second=100)
    power_curve.backfill_icu_history("default", max_per_second=100)
    assert len(asks) == 1, f"asked {len(asks)}x inside the cooldown"
    assert power_curve.count_rides_missing_efforts(3650)[1] == 0
    rec = json.loads(ride.read_text())
    assert rec.get("no_streams_available") is not True
    rs.update_json_record(ride, lambda r: {**r, "streams_retry_after": 0})   # cooldown over
    n = 700
    monkeypatch.setattr(tr, "fetch_activity_streams_result",
                        lambda _id: tr.Fetched("ok", {"watts": [200] * n}))
    power_curve.backfill_icu_history("default", max_per_second=100)
    rec = json.loads(ride.read_text())
    assert rec.get("efforts") and "streams_retry_after" not in rec
