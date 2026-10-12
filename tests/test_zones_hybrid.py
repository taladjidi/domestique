"""The one intensity scale and the one hybrid counter (contract slim/9.3, I1).

zones.goal_zone places a session on the planner's three zones (owner's D1);
zones.hybrid_minutes / hybrid_ride_minutes count every minute of a planned
workout or a ride by the session-goal / time-in-zone method (D2, Sylta 2014).
zones.tiz_minutes is the planner's own time in zone, checked against
zwo_parse.scan_zwo so the I0 measurement compares the hybrid with the numbers
the planner actually targets.
"""
import pathlib
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import zones  # noqa: E402
import zwo_parse  # noqa: E402


def _zwo(body: str) -> bytes:
    return f"<workout_file><name>t</name><workout>{body}</workout></workout_file>".encode()


@pytest.mark.parametrize("cc,st,want", [
    ("recovery", None, 1), ("endurance", None, 1), ("endurance_intervals", None, 1),
    (None, "z2", 1), (None, "long_z2", 1),
    ("tempo_ladder", None, 2), ("sweet_spot", None, 2), ("threshold", None, 2),
    ("over_under", None, 2), (None, "overunder", 2), (None, "sweetspot", 2),
    ("vo2max", None, 3), ("vo2_short", None, 3), ("anaerobic", None, 3),
    ("neuromuscular", None, 3), (None, "sprint", 3), ("ftp_test", None, 3),
    (None, "rest", None), (None, None, None), ("", "race", None),
])
def test_goal_zone(cc, st, want):
    assert zones.goal_zone(cc, st) == want


def test_goal_zone_reads_content_class_first():
    # The slot says z2, the served file is VO2max work: the file decides.
    assert zones.goal_zone("vo2max", "z2") == 3
    # An unknown class falls back to the session type.
    assert zones.goal_zone("mystery", "threshold") == 2


VO2 = _zwo('<Warmup Duration="600" PowerLow="0.5" PowerHigh="0.8"/>'
           '<IntervalsT Repeat="5" OnDuration="180" OffDuration="180" OnPower="1.00" OffPower="0.5"/>'
           '<SteadyState Duration="120" Power="0.0"/>'
           '<IntervalsT Repeat="2" OnDuration="60" OffDuration="60" OnPower="1.2" OffPower="0"/>'
           '<SteadyState Duration="300" Power="0.95"/>'
           '<Cooldown Duration="600" PowerLow="0.7" PowerHigh="0.4"/>')


def test_hybrid_counts_work_bouts_to_the_goal_zone():
    h = zones.hybrid_minutes(VO2, 3)
    # work: 5 x 3 min at 100% + 2 x 1 min at 120% + 5 min at 95% -> zone 3
    assert h["z3"] == pytest.approx(15 + 2 + 5)
    assert h["z2"] == 0
    # zone 1: warm-up 10 (part of it above 76%) + 5 x 3 active off + cool-down 10
    assert h["z1"] == pytest.approx(10 + 15 + 10)


def test_hybrid_does_not_count_passive_recovery():
    t, h = zones.tiz_minutes(VO2), zones.hybrid_minutes(VO2, 3)
    # time in zone counts every second; the 2 min at power 0 and the
    # 2 x 1 min OffPower=0 legs are the only minutes the hybrid leaves out
    assert sum(t.values()) - sum(h.values()) == pytest.approx(2 + 2)


def test_hybrid_an_off_leg_at_or_above_76_percent_is_work():
    # the unders of an over-under (0.90 FTP) are work bouts: goal zone, not
    # recovery (contract amendment 2026-10-12)
    b = _zwo('<IntervalsT Repeat="3" OnDuration="60" OffDuration="60" OnPower="1.10" OffPower="0.90"/>')
    assert zones.hybrid_minutes(b, 2) == pytest.approx({"z1": 0, "z2": 6, "z3": 0})
    # at exactly 76% too; below it the leg is active recovery
    b = _zwo('<IntervalsT Repeat="3" OnDuration="60" OffDuration="60" OnPower="1.10" OffPower="0.76"/>')
    assert zones.hybrid_minutes(b, 3) == pytest.approx({"z1": 0, "z2": 0, "z3": 6})
    b = _zwo('<IntervalsT Repeat="3" OnDuration="60" OffDuration="60" OnPower="1.10" OffPower="0.75"/>')
    assert zones.hybrid_minutes(b, 3) == pytest.approx({"z1": 3, "z2": 0, "z3": 3})


def test_hybrid_ramp_is_split_at_76_percent():
    # a work ramp from 66% to 86%: its lower half is zone 1, its upper the goal
    b = _zwo('<Ramp Duration="600" PowerLow="0.66" PowerHigh="0.86"/>')
    h = zones.hybrid_minutes(b, 2)
    assert h["z1"] == pytest.approx(5, abs=0.05)
    assert h["z2"] == pytest.approx(5, abs=0.05)


@pytest.mark.parametrize("goal", [1, None])
def test_easy_or_unknown_goal_is_time_in_zone(goal):
    b = _zwo('<SteadyState Duration="3000" Power="0.65"/>'
             '<SteadyState Duration="60" Power="1.3"/><SteadyState Duration="120" Power="0.9"/>')
    assert zones.hybrid_minutes(b, goal) == zones.tiz_minutes(b)
    assert zones.tiz_minutes(b) == pytest.approx({"z1": 50, "z2": 2, "z3": 1})


def test_tiz_minutes_is_scan_zwo(tmp_path):
    p = tmp_path / "w.zwo"
    p.write_bytes(VO2)
    r = zwo_parse.scan_zwo(p)
    t = zones.tiz_minutes(p)
    assert t["z1"] == pytest.approx((r["z1_sec"] + r["z2_sec"]) / 60)
    assert t["z2"] == pytest.approx((r["z3_sec"] + r["z4_sec"]) / 60)
    assert t["z3"] == pytest.approx((r["z5_sec"] + r["z6_sec"]) / 60)
    # the same answer from bytes, text, a path and a parsed element
    assert zones.tiz_minutes(VO2) == t == zones.tiz_minutes(VO2.decode())
    assert zones.tiz_minutes(zones._workout_element(VO2)) == t


TIZ = {"z1": 1200, "z2": 1800, "z3": 300, "z4": 600, "z5": 240, "z6": 60, "z7": 0, "ss": 999}


def test_ride_matched_to_a_hard_session_counts_its_work_to_the_goal():
    assert zones.hybrid_ride_minutes(TIZ, 3) == pytest.approx({"z1": 50, "z2": 0, "z3": 20})
    assert zones.hybrid_ride_minutes(TIZ, 2) == pytest.approx({"z1": 50, "z2": 20, "z3": 0})


@pytest.mark.parametrize("goal", [None, 1])
def test_ride_unmatched_or_easy_is_time_in_zone(goal):
    assert zones.hybrid_ride_minutes(TIZ, goal) == pytest.approx({"z1": 50, "z2": 15, "z3": 5})
    assert zones.hybrid_ride_minutes(None, goal) == {"z1": 0, "z2": 0, "z3": 0}
