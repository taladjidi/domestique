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


def test_power_is_counted_by_time_in_zone_every_second():
    """Contract amendment 2: a powered workout is counted second by second, no
    goal-zone credit: an over-under's 90% unders are zone 2 even in a VO2max
    session, a primer in the warm-up stays where its power is."""
    b = _zwo('<Warmup Duration="600" PowerLow="0.5" PowerHigh="1.0"/>'
             '<IntervalsT Repeat="3" OnDuration="60" OffDuration="60" OnPower="1.10" OffPower="0.90"/>')
    t = zones.tiz_minutes(b)
    assert t["z1"] == pytest.approx(10 * 0.52, abs=0.05)      # 50-76 % of the ramp
    assert t["z2"] == pytest.approx(10 * 0.48 + 3, abs=0.05)  # 76-100 % of it, the unders
    assert t["z3"] == pytest.approx(3)
    assert not hasattr(zones, "hybrid_minutes")


HR = {"z1": 600, "z2": 1200, "z3": 300, "z4": 300, "z5": 120, "z6": 60, "z7": 0}


def test_hr_only_ride_matched_counts_its_work_to_the_goal():
    # below the zone 1/2 seam (HR Z1-Z2) is zone 1, the rest to the goal zone
    assert zones.hr_hybrid_minutes(HR, 3) == pytest.approx({"z1": 30, "z2": 0, "z3": 13})
    assert zones.hr_hybrid_minutes(HR, 2) == pytest.approx({"z1": 30, "z2": 13, "z3": 0})


def test_hr_only_ride_unmatched_is_hr_time_in_zone():
    assert zones.hr_hybrid_minutes(HR, None) == pytest.approx({"z1": 30, "z2": 10, "z3": 3})
    assert zones.hr_hybrid_minutes(None, None) == {"z1": 0, "z2": 0, "z3": 0}


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




@pytest.mark.parametrize("goal,want,method", [
    (1, {"z1": 30, "z2": 10, "z3": 3}, "HR time in zone"),   # easy goal: surges not reclassified
    (2, {"z1": 30, "z2": 13, "z3": 0}, "hybrid (HR)"),
    (3, {"z1": 30, "z2": 0, "z3": 13}, "hybrid (HR)"),
])
def test_a_matched_hr_only_ride_for_each_goal_zone(goal, want, method):
    """A synthetic HR-only ride matched by completion to a session of goal
    zone 1, 2 or 3 (no owner HR-only ride is matched): only a hard goal gets
    the hybrid."""
    import week_view
    assert zones.hr_hybrid_minutes(HR, goal) == pytest.approx(want)
    m, how = week_view.ride_minutes({}, HR, goal)
    assert how == method
    assert m == pytest.approx({f"zone{k[1]}": v for k, v in want.items()})
