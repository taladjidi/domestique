"""A ride's duration is how long the rider rode: moving time, as intervals.icu
shows it.

Reported: yesterday's ride is 1h31 on intervals.icu and 118 min in Domestique.
The stored record has both -- duration_s 7080 (elapsed) and moving_s 5515
(moving, 1h31:55) -- and the calendar read the elapsed one, while the zone
minutes on the same card (65.6 + 20.6 + 5.7) summed to 91.9: a card that
disagreed with itself. The calendar's plan comparison inherited it, judging
the ride +43 min over a 75-min plan when it was +17.

The numbers below are that ride's.
"""
from __future__ import annotations

import ride_storage as rs

ICU_RIDE = {
    "ride_id": "icu_i187384473", "source": "icu", "name": "Afternoon Ride",
    "started_at": "2026-09-16T14:07:32",
    "duration_s": 7080,          # elapsed: includes the stops
    "moving_s": 5515,            # what intervals.icu displays: 1h31
    "tss": 87.0, "avg_power_w": 158,
    "time_in_zone": {"z1": 1908, "z2": 2028, "z3": 900, "z4": 336,
                     "z5": 240, "z6": 72, "z7": 30},
}
FIT_RIDE = {                     # a FIT import: duration_s is timer time already
    "ride_id": "fit_abcd1234", "source": "fit", "duration_s": 3600,
}


def test_the_one_definition():
    assert rs.ride_moving_s(ICU_RIDE) == 5515, "an ICU ride's duration is its moving time"
    assert rs.ride_moving_s(FIT_RIDE) == 3600, "a FIT ride's timer time is already moving time"
    assert rs.ride_moving_s({"ride_id": "x", "duration_s": 7080}) == 7080, (
        "with no moving time recorded, elapsed is the best there is")
    assert rs.ride_moving_s({}) == 0
    assert rs.ride_moving_s(None) == 0


def test_the_calendar_card_shows_moving_time_and_agrees_with_itself():
    import app
    card = app._summarize_ride_for_calendar(ICU_RIDE, ftp=247)
    assert card["duration_min"] == round(5515 / 60, 1), (
        f"calendar says {card['duration_min']} min; intervals.icu says 1h31")
    zones = card["zone1_min"] + card["zone2_min"] + card["zone3_min"]
    assert abs(card["duration_min"] - zones) <= 1.0, (
        f"the card's duration ({card['duration_min']}) and its own zone minutes "
        f"({zones:.1f}) must describe the same ride")


def test_the_plan_comparison_judges_the_ride_actually_ridden():
    """75 min planned, 92 ridden: over by 17, not by 43."""
    import app
    planned = {"session_type": "z2", "duration_min": 75, "tss_estimate": 60}
    card = app._summarize_ride_for_calendar(ICU_RIDE, ftp=247, planned_session=planned)
    cmp = card.get("compare") or {}
    delta = cmp.get("duration_delta_min", cmp.get("duration_delta"))
    assert delta is not None, f"no duration delta in {sorted(cmp)}"
    assert abs(float(delta) - (5515 / 60 - 75)) <= 1.0, (
        f"compared against elapsed time: delta {delta} min")


def test_the_ride_modal_time_tile_reads_moving_time():
    import pathlib
    html = (pathlib.Path(rs.__file__).resolve().parent / "templates" / "dashboard.html").read_text()
    assert "{l:'Time',     v: fmtDur(p.duration_s)}" not in html, (
        "the ride modal's Time tile is reading elapsed time")
