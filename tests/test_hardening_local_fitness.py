"""A rider without intervals.icu: CTL and ATL from one set of rides, and the
week's rides visible to the week-activity collector (contract 5.2).

The audit (2026-09-23, rv-facts #1, rv-struct #2) built a FIT-only rider:
rides imported as .fit files with their load sidecars, no intervals.icu.
  * compute_local_ctl read list_rides() -- the legacy ride_*.json summaries
    that nothing writes any more -- while compute_local_atl read
    load_all_rides(). CTL came out None (fitness "unknown") or, with one
    stray legacy file, 1.9 against an ATL of 52.9: a TSB of -51.
  * app._collect_week_activities (daily-adapt, rematch, missed-session
    detection) read the activities table and list_rides(): for this rider
    the week was [] and every ridden session looked missed.

compute_local_ctl now reads load_all_rides() like ATL does, and
ride_storage.activity_rows_between() gives the collector the same rides in
the shape it reads (app.py switches to it in Wave 2).
"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from unittest import mock

import pytest

import clock
import db
import fitness
import ride_storage as rs
import training
from profile_manager import ProfileManager


@pytest.fixture
def rider(tmp_path, monkeypatch):
    p = mock.patch("pathlib.Path.home", return_value=tmp_path)
    p.start()
    old = ProfileManager._instance
    ProfileManager._instance = None
    old_db = db.DB_PATH
    pm = ProfileManager.get()
    pm.switch(pm.create_profile("FitOnly"))
    db._sync_stop.clear()
    monkeypatch.setattr(training, "_require_credentials",
                        lambda: (_ for _ in ()).throw(training.ICUCredentialsMissing("no ICU")))
    yield pm
    p.stop()
    ProfileManager._instance = old
    db.set_db_path(old_db)


def _import_fit(day: dt.date, tss: float, hour: int = 8, secs: int = 3600):
    """A FIT import as load_all_rides sees it: the file plus its sidecar."""
    stem = f"{day.isoformat()}T{hour:02d}-00-00"
    d = rs._fit_rides_dir()
    (d / f"{stem}.fit").write_bytes(b"\x0e\x10")
    (d / f"{stem}.load.json").write_text(json.dumps({
        "tss": tss, "load_source": "power", "duration_s": secs,
        "started_at": f"{day.isoformat()}T{hour:02d}:00:00",
        "reader_v": rs.FIT_LOAD_READER_V}))


def _ewma(per_day: dict, tau: float, today: dt.date) -> float:
    v, d = 0.0, min(per_day)
    while d <= today:
        v += (per_day.get(d, 0.0) - v) / tau
        d += dt.timedelta(days=1)
    return round(v, 1)


def _forty_days(today):
    per_day = {}
    for k in range(1, 41):
        day = today - dt.timedelta(days=k)
        tss = 50.0 + (k % 4) * 10
        _import_fit(day, tss)
        per_day[day] = tss
    return per_day


def test_a_fit_only_rider_has_a_ctl_and_atl_from_the_same_rides(rider):
    today = clock.today()
    per_day = _forty_days(today)
    st = fitness.state(None, today)
    assert st["source"] == "local", st
    assert st["ctl"] == _ewma(per_day, 42, today)
    assert st["atl"] == _ewma(per_day, 7, today)
    assert st["tsb"] == round(st["ctl"] - st["atl"], 1)


def test_a_stray_legacy_summary_does_not_split_the_two(rider):
    """The audit's control: one legacy ride_*.json made list_rides() non-empty
    and CTL read 1.9 against an ATL of 52.9."""
    today = clock.today()
    per_day = _forty_days(today)
    legacy = today - dt.timedelta(days=14)
    (rs._rides_dir() / f"ride_{legacy.isoformat()}T07-00-00.json").write_text(json.dumps({
        "id": "legacy1", "started_at": f"{legacy.isoformat()}T07:00:00",
        "finished_at": f"{legacy.isoformat()}T08:20:00", "summary": {"tss": 80}}))
    assert rs.compute_local_ctl() == _ewma(per_day, 42, today)


def test_compute_local_ctl_takes_the_callers_rides(rider):
    today = clock.today()
    rides = [{"started_at": f"{(today - dt.timedelta(days=2)).isoformat()}T08:00:00", "tss": 100}]
    assert rs.compute_local_ctl(rides=rides, today=today) == round(
        100 / 42 * (1 - 1 / 42) ** 2, 1)


# ── the week-activity collector's input ──────────────────────────────────────

def _this_week(today):
    ws = today - dt.timedelta(days=today.weekday())
    _import_fit(ws, 70.0, hour=7, secs=5400)
    _import_fit(today, 55.0, hour=18, secs=3000)
    _import_fit(ws - dt.timedelta(days=1), 90.0)        # last week: not in it
    return ws


def test_activity_rows_between_lists_this_weeks_rides(rider):
    today = clock.today()
    ws = _this_week(today)
    rows = rs.activity_rows_between(ws, today + dt.timedelta(days=1))
    assert sorted((r["date"], r["tss"], r["duration_min"]) for r in rows) == sorted(
        [(ws.isoformat(), 70.0, 90.0), (today.isoformat(), 55.0, 50.0)])


def test_the_rows_are_what_the_collector_reads(rider, monkeypatch):
    """Fed through app._collect_week_activities in place of list_rides(), the
    rows come out as this week's rides (the shape check for Wave 2)."""
    import app as app_module
    today = clock.today()
    ws = _this_week(today)
    monkeypatch.setattr(rs, "list_rides", lambda: rs.activity_rows_between(
        ws, today + dt.timedelta(days=1)))
    got = app_module._collect_week_activities(SimpleNamespace(start=ws), today,
                                              include_today=True)
    assert sorted((a["date"], a["tss"], a["duration_min"]) for a in got) == sorted(
        [(ws.isoformat(), 70.0, 90.0), (today.isoformat(), 55.0, 50.0)])


@pytest.mark.xfail(strict=True, reason=(
    "Wave 2: app._collect_week_activities must read "
    "ride_storage.activity_rows_between (notes/hardening-interfaces-storage.md)"))
def test_the_collector_itself_sees_a_fit_only_riders_week(rider):
    import app as app_module
    today = clock.today()
    ws = _this_week(today)
    got = app_module._collect_week_activities(SimpleNamespace(start=ws), today,
                                              include_today=True)
    assert len(got) == 2
