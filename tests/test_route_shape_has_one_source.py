"""A route's climb is read from the course file, on every screen.

Two screens showed one hill. `/api/courses` computed distance and climb from
the `.crs` data with a comment warning never to trust the DESCRIPTION header,
while `/api/routes` served whatever `routes.json` had stored -- and the stored
index inherited exactly the kind of number that comment warns about.

Measured across the shipped library: 74 of the 622 routes that have a course
file disagreed, worst by 1,148 m of climbing (Mallorca 312 stored as 3,503 m
against 4,651 m in the course data; the event itself advertises about 4,700).

Now `routes_lib.crs_profile` is the one reader and the index is reconciled
against the course files on load, so both screens answer from the same place.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import routes_lib                                     # noqa: E402

TOLERANCE_M = 5.0


def _routes_with_a_course_file():
    routes, _index = routes_lib._load_routes_v2(force=True)
    out = []
    for route in routes:
        profile = routes_lib._profile_from_disk(route)
        if profile:
            out.append((route, profile))
    return out


def test_the_library_has_course_files_to_check():
    pairs = _routes_with_a_course_file()
    assert len(pairs) > 500, (
        f"only {len(pairs)} routes resolved to a course file; this test would "
        f"pass by checking almost nothing")


def test_every_route_agrees_with_its_course_file_about_the_climb():
    disagreeing = [
        (route.get("id"), route.get("climb_m"), profile["climb_m"])
        for route, profile in _routes_with_a_course_file()
        if route.get("climb_m") is None
        or abs(float(route["climb_m"]) - profile["climb_m"]) > TOLERANCE_M
    ]
    assert disagreeing == [], (
        f"{len(disagreeing)} routes disagree with their own course file, "
        f"worst: {sorted(disagreeing, key=lambda r: -abs((r[1] or 0) - r[2]))[:3]}")


def test_a_planted_wrong_climb_is_corrected(monkeypatch):
    """The control. Reconciliation that cannot notice a wrong number is not
    reconciliation -- and this is what the shipped index looked like for 74
    routes."""
    routes, _ = routes_lib._load_routes_v2(force=True)
    victim = next(r for r in routes if routes_lib._profile_from_disk(r))
    real = routes_lib._profile_from_disk(victim)["climb_m"]
    victim["climb_m"] = real + 900
    moved = routes_lib._reconcile_with_course_files([victim])
    assert moved == 1, "the reconciler ignored a 900 m error"
    assert victim["climb_m"] == real


def test_both_readers_are_the_same_function():
    """/api/courses used to carry its own copy of the parser."""
    import inspect

    import app

    source = inspect.getsource(app.api_courses)
    assert "crs_profile" in source, "the courses endpoint stopped using the shared reader"
    assert "[COURSE DATA]" not in source, (
        "the courses endpoint is parsing the course file itself again; that is "
        "the copy that drifted from routes.json in the first place")
