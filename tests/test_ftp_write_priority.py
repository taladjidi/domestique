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
    """The 7-day drift rule, as the sync runs it, twice: a tested FTP survives.

    The auto path used to ignore update_ftp's refusal, append an applied
    ledger row, and re-stamp `ftp_source` "eftp_auto" on athlete.json by hand.
    The first sync left 300 standing but relabelled it an estimate; the second
    then met an equal tier and wrote 330 over the test. This drives the real
    rule (it used to call a helper that does not exist, and skipped every run).
    """
    import training_planner as tp

    pm.update_ftp(300, source="tested_coggan_20min")
    pm.save_prefs({"eftp_auto_apply": True})
    # ten days of eFTP 330: past the +3% threshold and the 7-day streak, and
    # plausible (the app's guard only rejects <100 W or <60 % of current).
    series = [{"id": f"2026-09-{d:02d}", "sportInfo": [{"eftp": 330}]}
              for d in range(10, 20)]
    for sync in (1, 2):
        out = tp.check_and_auto_apply_eftp(series)
        assert out is None, f"sync {sync}: the rule reported {out} for a refused write"
        assert (pm.ftp, pm.ftp_source) == (300, "tested_coggan_20min"), (
            f"sync {sync}: FTP {pm.ftp} from {pm.ftp_source!r}; the rider tested 300")
    assert not [h for h in pm.ftp_test_history if h.get("source") == "eftp_auto"], (
        "a refused write left an eftp_auto row in the FTP ledger")


def test_the_drift_rule_still_applies_over_an_estimate(pm):
    """Control: the gate refuses by tier, it does not switch the rule off."""
    import training_planner as tp

    pm.update_ftp(240, source="eftp_icu")
    pm.save_prefs({"eftp_auto_apply": True})
    series = [{"id": f"2026-09-{d:02d}", "sportInfo": [{"eftp": 262}]}
              for d in range(10, 20)]
    out = tp.check_and_auto_apply_eftp(series)
    assert out and out["applied"] and out["new_ftp"] == 262
    assert (pm.ftp, pm.ftp_source) == (262, "eftp_auto")
    assert [h for h in pm.ftp_test_history if h.get("source") == "eftp_auto"]
