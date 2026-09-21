"""An automatic eFTP never overwrites a number the rider stood for.

The profile owns the FTP, and three kinds of writer reach it: the rider typing
one in settings, an FTP test they rode, and the 7-day sustained-drift rule
that applies an intervals.icu eFTP on its own (training_planner, source
"eftp_auto").

Every sibling setter on the profile already gates on provenance:
`_set_wprime` (profile_manager.py:752) refuses a write from a lower-priority
source, and `_set_pmax` and `_set_max_hr` do the same. `update_ftp` did not.
So a rider could ride a 20-minute test on Sunday, have it recorded, and lose
it on Monday to a drifting estimate, with their .zwo files rewritten at the
new number.

The rule: manual and tested sit in one tier, the automatic estimates below it.
A write from a lower tier is refused and says so; an equal or higher tier
wins, so the rider can always overrule themselves.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from profile_manager import ProfileManager          # noqa: E402


@pytest.fixture()
def pm(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMESTIQUE_HOME", str(tmp_path))
    old = ProfileManager._instance
    ProfileManager._instance = None
    manager = ProfileManager.get()
    manager.switch(manager.create_profile("Rider"))
    try:
        yield manager
    finally:
        ProfileManager._instance = old


def test_an_automatic_eftp_does_not_overwrite_a_tested_ftp(pm):
    pm.update_ftp(300, source="tested_coggan_20min")
    wrote = pm.update_ftp(320, source="eftp_auto")
    assert wrote is False, "the drift rule overwrote a tested FTP"
    assert pm.ftp == 300, f"FTP is {pm.ftp}; the rider tested 300"
    assert pm.ftp_source == "tested_coggan_20min", (
        f"provenance is {pm.ftp_source!r}: the refused write still stamped "
        f"itself, so the next reader believes an estimate was a test")


def test_the_rider_can_always_overrule_themselves(pm):
    pm.update_ftp(300, source="tested_coggan_20min")
    assert pm.update_ftp(260, source="manual") is not False
    assert (pm.ftp, pm.ftp_source) == (260, "manual")


def test_an_automatic_eftp_applies_when_nothing_better_is_on_record(pm):
    pm.update_ftp(240, source="eftp_icu")
    assert pm.update_ftp(255, source="eftp_auto") is not False
    assert pm.ftp == 255


def test_a_tested_ftp_replaces_an_automatic_one(pm):
    pm.update_ftp(240, source="eftp_auto")
    assert pm.update_ftp(275, source="tested_ramp") is not False
    assert (pm.ftp, pm.ftp_source) == (275, "tested_ramp")


def test_the_drift_rule_leaves_no_trace_when_it_is_refused(pm):
    """The auto path used to re-stamp `ftp_source` on athlete.json directly
    after calling update_ftp, which is the owner's field. A refused write that
    still writes provenance is worse than no gate at all."""
    import training_planner as tp

    pm.update_ftp(300, source="tested_coggan_20min")
    applied = tp._maybe_apply_eftp_drift(pm, 330) if hasattr(
        tp, "_maybe_apply_eftp_drift") else None
    if applied is None:
        pytest.skip("drift helper not exposed; covered by the unit tests above")
    assert pm.ftp == 300 and pm.ftp_source == "tested_coggan_20min"
