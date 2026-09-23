"""Contract 5.5: eFTP, W' and Pmax come from the cycling entry of
intervals.icu's `sportInfo`, read by one helper (training.cycling_sport_info)
that every reader uses; a Run entry listed first changes nothing.

Wave 1 switched db.sync_wellness and ride_storage.latest_eftp. app.py kept
eight hand-copied `sportInfo[0]` reads and training_planner one: with a Run
eFTP (330) listed first, the weekly-plan banner said "update your FTP +32%",
the setup prefill offered 330, the auto-recalc planned from it (audit
2026-09-23, rv-facts #4).
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
import config
import training_planner as tp

SRC = Path(app_module.__file__).parent
RUN_FIRST = [{"type": "Run", "eftp": 330, "wPrime": 30000, "pMax": 900},
             {"type": "Ride", "eftp": 250, "wPrime": 20000, "pMax": 1100}]


def _days(n=7, info=RUN_FIRST):
    t = date(2026, 9, 20)
    return [{"id": (t - timedelta(days=i)).isoformat(), "sportInfo": [dict(e) for e in info]}
            for i in reversed(range(n))]


@pytest.mark.parametrize("module", ["app.py", "training_planner.py"])
def test_no_reader_indexes_sport_info_itself(module):
    src = (SRC / module).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert re.findall(r'\.get\(\s*"sportInfo"', code) == []
    assert re.findall(r'\[0\]\s*(\.get\(\s*"eftp"|\[\s*"eftp")', code) == []


def test_the_wellness_api_reports_the_cycling_eftp():
    assert app_module._wellness_record_to_api_dict(_days(1)[0])["eftp"] == 250


def test_the_weekly_banner_does_not_see_a_run_eftp(monkeypatch):
    monkeypatch.setattr(config, "ATHLETE_FTP_W", 250, raising=False)
    app_module.clear_cache()
    with patch.object(app_module, "fetch_wellness", lambda days=7, **k: _days(days)):
        r = TestClient(app_module.app).get("/api/weekly-plan")
    app_module.clear_cache()
    assert r.status_code == 200
    assert "eftp_drift" not in r.json(), r.json().get("eftp_drift")


def test_the_athlete_numbers_prefill_offers_the_cycling_eftp(monkeypatch):
    import training
    monkeypatch.setattr(config, "ICU_API_KEY", "k", raising=False)
    monkeypatch.setattr(training, "_get", lambda *a, **k: {"sportSettings": []})
    app_module.clear_cache()
    with patch.object(app_module, "fetch_wellness", lambda days=7, **k: _days(days)):
        out = app_module.api_icu_athlete_numbers()
    app_module.clear_cache()
    assert out["ftp"] == 250


def test_the_auto_apply_streak_reads_the_cycling_eftp(monkeypatch):
    """FTP 250: the ride eFTP 250 is no drift; the run's 330 was a 7-day
    +32% streak that auto-applied 330."""
    from profile_manager import ProfileManager
    monkeypatch.setattr(config, "ATHLETE_FTP_W", 250, raising=False)
    pm = ProfileManager.get()
    monkeypatch.setattr(type(pm), "prefs", property(lambda self: {"eftp_auto_apply": True}))
    applied = []
    for name in ("record_ftp_test", "update_ftp", "_write_json"):
        monkeypatch.setattr(type(pm), name,
                            lambda self, *a, _n=name, **k: applied.append((_n, a, k)))
    assert tp.check_and_auto_apply_eftp(_days(8)) is None
    assert applied == []
    # control: a ride eFTP above +3% for 8 days does apply, the ride's number
    rides_up = [{"type": "Run", "eftp": 330}, {"type": "Ride", "eftp": 265}]
    out = tp.check_and_auto_apply_eftp(_days(8, rides_up))
    assert out and out["new_ftp"] == 265


def test_the_guard_in_front_of_auto_apply_reads_the_cycling_eftp(monkeypatch):
    """The plausibility guard pre-validates the newest record's eFTP; with a
    Run first it vetted the wrong number."""
    monkeypatch.setattr(config, "ATHLETE_FTP_W", 250, raising=False)
    seen = []
    monkeypatch.setattr(app_module, "_ftp_auto_ingest_ok",
                        lambda new, cur: seen.append(new) or True)
    monkeypatch.setattr(tp, "check_and_auto_apply_eftp", lambda series: None)
    app_module._guarded_check_and_auto_apply_eftp(_days(3))
    assert seen == [250]


# ── fix-forward F5 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("info,want", [
    # a Ride entry without eFTP beside a VirtualRide that has it
    ([{"type": "Ride", "wPrime": 20000}, {"type": "VirtualRide", "eftp": 262}], 262),
    # gravel and MTB are cycling too
    ([{"type": "Run", "eftp": 330}, {"type": "GravelRide", "eftp": 241}], 241),
    ([{"type": "Run", "eftp": 330}, {"type": "MountainBikeRide", "eftp": 238}], 238),
    # control: two cycling entries with a value -> the first listed
    ([{"type": "Ride", "eftp": 250}, {"type": "VirtualRide", "eftp": 262}], 250),
    # control: no cycling entry at all
    ([{"type": "Run", "eftp": 330}, {"type": "Swim"}], None),
])
def test_the_cycling_entry_is_any_ride_type_and_the_one_with_the_value(info, want):
    """Fix-forward F5: only Ride / VirtualRide counted, and the first of them
    won even when it carried no eFTP -- a Ride without eFTP hid the
    VirtualRide eFTP, and a gravel-only rider had none."""
    import training
    ci = training.cycling_sport_info({"sportInfo": info})
    assert ci.get("eftp") == want, ci
    if len(info) == 2 and info[0].get("wPrime"):
        assert ci.get("wPrime") == 20000      # each value from the entry carrying it
