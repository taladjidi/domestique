"""FTP has one owner: the profile.

`config.ATHLETE_FTP_W` is not a value, it is a PROXY: config has no such
attribute, and `config.__getattr__` (config.py:118) resolves it through
ProfileManager on every read. That is what makes the profile the owner.

Python consults `__getattr__` only when the module has no real attribute, so
ONE assignment turns the proxy off permanently -- for the life of the process
and across profile switches, with nothing to clear it. `/api/profile/update-ftp`
did exactly that (app.py, "mirror onto the LIVE config"), and the comment
explains why: it was written when config cached its values at startup, before
the proxy existed. After it, a rider who tested at 300 and then set 260 in
settings got 300 in the FTP field and 260 in the power zones OF THE SAME
RESPONSE, and their .zwo files were written at 300.

training.py's credential swaps face the same hazard and already handle it:
they assign, then `del` in a `finally`, with the reason in a comment
(v4.5.2 FIX-CREDS-HOTRELOAD). This file holds that rule for every proxied
name, so the next mirror is a red test rather than a rider's bug report.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import config                                        # noqa: E402
import app as app_module                             # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402
from profile_manager import ProfileManager           # noqa: E402

# Every name config resolves through the profile (config.py:125-132).
PROXIED = ("ATHLETE_FTP_W", "ATHLETE_WEIGHT_KG", "ATHLETE_LBM_KG",
           "ATHLETE_LTHR", "ATHLETE_MAX_HR", "HRV_BASELINE_MEAN",
           "HRV_BASELINE_SD", "RHR_BASELINE", "ICU_ATHLETE_ID",
           "ICU_API_KEY", "ICU_ACCESS_TOKEN")


@pytest.fixture()
def rider(tmp_path, monkeypatch):
    """A profile with an FTP, and the app pointed at it."""
    monkeypatch.setenv("DOMESTIQUE_HOME", str(tmp_path))
    old_instance, old_data = ProfileManager._instance, app_module.DATA_DIR
    ProfileManager._instance = None
    app_module.DATA_DIR = tmp_path / ".domestique"
    pm = ProfileManager.get()
    pid = pm.create_profile("Rider")
    pm.switch(pid)
    pm.update_ftp(250, source="manual")
    shadowed = [n for n in PROXIED if n in vars(config)]
    for n in shadowed:                       # a previous test may have left one
        delattr(config, n)
    try:
        yield pm, TestClient(app_module.app)
    finally:
        for n in PROXIED:
            if n in vars(config):
                delattr(config, n)
        ProfileManager._instance = old_instance
        app_module.DATA_DIR = old_data


def test_the_proxy_is_never_shadowed_by_a_real_attribute(rider):
    """The rule, stated once. A real attribute beats __getattr__, so any
    module-level assignment to a proxied name silently makes the profile stop
    being the owner."""
    pm, client = rider
    r = client.post("/api/profile/update-ftp",
                    json={"ftp": 300, "method": "coggan_20min", "applied": True})
    assert r.status_code == 200, r.text
    left = [n for n in PROXIED if n in vars(config)]
    assert left == [], (
        f"{left} became real module attributes, so config.__getattr__ no "
        f"longer resolves them through the profile. Refresh by writing to the "
        f"profile (pm.update_ftp); the proxy reflects it on the next read.")


def test_every_reader_follows_the_profile_after_a_test_then_a_settings_change(rider):
    """The rider's sequence: test at 300, then set 260 in settings."""
    pm, client = rider
    client.post("/api/profile/update-ftp",
                json={"ftp": 300, "method": "coggan_20min", "applied": True})
    assert config.ATHLETE_FTP_W == 300

    pm.update_ftp(260, source="manual")              # what a settings save does
    assert pm.ftp == 260
    assert config.ATHLETE_FTP_W == 260, (
        "config still reports the tested FTP after the profile moved on: the "
        "proxy has been shadowed, and every reader that goes through config "
        "(power zones, .zwo watts) now disagrees with the profile")

    settings = client.get("/api/settings").json()
    assert settings.get("ftp") == 260, f"/api/settings.ftp says {settings.get('ftp')}"


def test_the_proxy_follows_a_profile_switch(rider):
    """Two riders, two FTPs. A shadowed attribute survives the switch and
    hands rider B rider A's numbers."""
    pm, client = rider
    client.post("/api/profile/update-ftp",
                json={"ftp": 300, "method": "coggan_20min", "applied": True})
    other = pm.create_profile("Other")
    pm.switch(other)
    pm.update_ftp(190, source="manual")
    assert config.ATHLETE_FTP_W == 190, (
        f"after switching profiles config reports {config.ATHLETE_FTP_W}, "
        f"which belongs to the previous rider")
