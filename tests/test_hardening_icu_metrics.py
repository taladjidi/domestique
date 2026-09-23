"""intervals.icu never overwrites a metric the rider logged (contract 2.3),
and reads the CYCLING entry of sportInfo (contract 5.5, the db side).

2.3 -- The audit (2026-09-23, data #7) logged a lab VO2max of 61.5 through
/api/metrics/log and synced: sync_wellness wrote Garmin's 54.0 over it with
`INSERT OR REPLACE` and no source check, on a table whose row carries the
rider's own `source` and `notes`. Four sibling upserts (eFTP, W', Pmax, max
HR) each carried a hand-copied "not manual" guard -- which still let a
settings-sourced value be overwritten. One helper now owns the rule: an
intervals.icu value replaces only an intervals.icu value.

5.5 -- sportInfo lists one entry per sport. Every reader took entry [0]; for
a rider whose first entry is Run, a run eFTP of 330 was stored as the
cycling eFTP (audit rv-facts #4: a +32% "update your FTP" banner).
"""
from __future__ import annotations

from unittest import mock

import pytest

import db
import training as tr

D = "2026-09-22"


@pytest.fixture()
def fresh_db(tmp_path):
    original = db.DB_PATH
    db.set_db_path(tmp_path / "health_tracker.db")
    db.close_all_connections()
    db.init_db()
    try:
        yield
    finally:
        db.set_db_path(original)
        db.close_all_connections()


def _sync(wellness):
    with mock.patch.object(db, "fetch_wellness", lambda days=90: wellness):
        db.sync_wellness(90)


def _metric(name):
    row = db.get_db().execute(
        "SELECT value, source, notes FROM athlete_metrics WHERE date=? AND metric=?",
        (D, name)).fetchone()
    return None if row is None else tuple(row)


ICU_DAY = [{"id": D, "ctl": 60, "atl": 55, "vo2max": 54.0, "maxHr": 185,
            "sportInfo": [{"type": "Ride", "eftp": 262.4, "wPrime": 18000, "pMax": 900}]}]


@pytest.mark.parametrize("source", ["manual", "settings"])
@pytest.mark.parametrize("metric,value", [("vo2max", 61.5), ("eftp", 290.0),
                                          ("w_prime", 21000.0), ("pmax", 1100.0),
                                          ("max_hr", 192.0)])
def test_a_rider_logged_metric_survives_the_sync(fresh_db, source, metric, value):
    db.log_metric(D, metric, value, source=source, notes="lab test")
    _sync(ICU_DAY)
    assert _metric(metric) == (value, source, "lab test")


def test_icu_values_still_land_and_update_themselves(fresh_db):
    _sync(ICU_DAY)
    assert _metric("vo2max") == (54.0, "intervals.icu", None)
    assert _metric("eftp") == (262.0, "intervals.icu", None)
    _sync([{**ICU_DAY[0], "vo2max": 55.0,
            "sportInfo": [{"type": "Ride", "eftp": 265.6}]}])
    assert _metric("vo2max") == (55.0, "intervals.icu", None)
    assert _metric("eftp") == (266.0, "intervals.icu", None)


def test_a_run_listed_first_changes_nothing(fresh_db):
    run = {"type": "Run", "eftp": 330, "wPrime": 25000, "pMax": 1500}
    ride = {"type": "Ride", "eftp": 262, "wPrime": 18000, "pMax": 900}
    _sync([{"id": D, "ctl": 60, "atl": 55, "sportInfo": [run, ride]}])
    assert _metric("eftp")[0] == 262
    assert _metric("w_prime")[0] == 18000
    assert _metric("pmax")[0] == 900
    eftp = db.get_db().execute("SELECT eftp FROM wellness WHERE date=?", (D,)).fetchone()[0]
    assert eftp == 262


# ── the helper itself ────────────────────────────────────────────────────────

@pytest.mark.parametrize("info,expected", [
    ([{"type": "Run", "eftp": 330}, {"type": "Ride", "eftp": 262}], 262),
    ([{"type": "Run", "eftp": 330}, {"type": "VirtualRide", "eftp": 255}], 255),
    ([{"type": "Ride", "eftp": 262}, {"type": "Run", "eftp": 330}], 262),
    ([{"type": "Run", "eftp": 330}], None),
    ([{"eftp": 262}], 262),                 # legacy payloads carry no type
    ([], None),
    (None, None),
    ("garbage", None),
])
def test_cycling_sport_info(info, expected):
    got = tr.cycling_sport_info({"sportInfo": info})
    assert got.get("eftp") == expected


def test_latest_eftp_reads_the_cycling_entry(tmp_path, monkeypatch):
    import json
    import ride_storage as rs
    monkeypatch.setattr(rs, "_wellness_dir", lambda: tmp_path)
    (tmp_path / f"{D}.json").write_text(json.dumps(
        {"id": D, "sportInfo": [{"type": "Run", "eftp": 330},
                                {"type": "Ride", "eftp": 262}]}))
    assert rs.latest_eftp() == 262.0
