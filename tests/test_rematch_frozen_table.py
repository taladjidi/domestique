"""Completion matching keeps its behaviour on its own frozen table (contract
slim/9.3, I3).

classify_rematch's IF axis compares the session's band with the ride's IF
band (0.65 / 0.82 / 0.97). It read week_view.TYPE_BAND, the table the
dashboard's exposure bars read; when the display moved to the planner's three
zones, matching had to keep its answer exactly (changing it is phase B,
measured first). So it reads training_planner._REMATCH_TYPE_BAND, and nothing
the display owns can move it.
"""
from datetime import date

import pytest

import training_planner as tp
import week_view

# The table as week_view.TYPE_BAND held it at 574d844b, byte for byte.
FROZEN = {
    "recovery": "low_aerobic", "z2": "low_aerobic", "long_z2": "low_aerobic",
    "endurance": "low_aerobic", "tempo": "mid_aerobic", "sweetspot": "high_aerobic",
    "threshold": "high_aerobic", "ftp_test": "high_aerobic", "overunder": "anaerobic",
    "vo2max": "anaerobic", "anaerobic": "anaerobic", "sprint": "anaerobic",
    "neuromuscular": "anaerobic",
}


def _session(st):
    return tp.PlannedSession(day=date(2026, 10, 13), day_name="Tue", session_type=st,
                             duration_min=60, tss_estimate=80, description="")


def test_the_table_is_frozen():
    assert tp._REMATCH_TYPE_BAND == FROZEN


@pytest.mark.parametrize("st,if_,band_ok", [
    ("vo2max", 0.90, False),       # a VO2max day ridden at IF 0.90 still fails the axis
    ("vo2max", 1.00, True),
    ("threshold", 0.90, True),
    ("tempo", 0.75, True),
    ("z2", 0.60, True),
    ("overunder", 0.90, False),
    ("rest", 0.60, False),
])
def test_if_axis_answers_as_before(st, if_, band_ok):
    r = tp.classify_rematch(_session(st), {"tss": 80, "duration_min": 60, "intensity_factor": if_})
    assert r["axes"]["if_band_ok"] is band_ok
    assert r["details"]["planned_band"] == FROZEN.get(st)


def test_matching_does_not_read_the_display_table(monkeypatch):
    """Whatever the week view's display tables become, matching answers the
    same."""
    monkeypatch.setattr(week_view, "TYPE_BAND", {k: "zone3" for k in FROZEN}, raising=False)
    r = tp.classify_rematch(_session("threshold"), {"tss": 80, "duration_min": 60,
                                                     "intensity_factor": 0.90})
    assert r["axes"]["if_band_ok"] is True
    assert r["status"] == "done"
