"""The ACWR report reaches someone.

`_report_acwr` runs `plan_invariants.check_acwr` against the load the rider
carries in and logs a line per violation. Its return value is discarded at the
call site and nothing captured the log, so a mutation sweep found the call
could be deleted from `generate_plan` with the whole suite green: the helper
had tests, its WIRING had none, and the only evidence it ever ran was a line
in a file nobody reads.

A report nobody can observe is not a report. These assert the log line
actually comes out of a real generate for a rider the rule is about -- a
detrained one, carrying little, ramped hard -- and stays silent for a fit one.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import clock                                          # noqa: E402
import training_planner as tp                         # noqa: E402

TODAY = date(2026, 9, 21)                             # a Monday


@pytest.fixture(autouse=True)
def _frozen():
    clock.freeze(TODAY)
    yield
    clock.unfreeze()


def _goal():
    return tp.Goal(goal_type="continuous", target_date=None, hours_per_week=8.0)


def _generate(carried, caplog):
    caplog.set_level(logging.INFO, logger="domestique.planner")
    with caplog.at_level(logging.INFO):
        tp.generate_plan(_goal(), current_ctl=45.0, recent_weekly_tss=carried)
    return [r.getMessage() for r in caplog.records
            if "acwr_over_sweet_spot" in r.getMessage()]


def test_a_detrained_rider_ramped_hard_is_reported(caplog):
    """Carrying 180 TSS a week and handed a full plan: the ratio the rule
    exists for. The number is the rider's own recent load, which is why the
    check could not live in ALL_CHECKS -- those take a plan and no rider."""
    said = _generate(180.0, caplog)
    assert said, (
        "generate_plan produced a plan for a rider carrying 180 TSS/week and "
        "said nothing about the ramp; either the call was dropped or the rule "
        "stopped firing")
    assert "week=" in said[0]


def test_a_fit_rider_is_not_nagged(caplog):
    """The guard on the guard: a report that always fires is noise, and noise
    is how a real one gets ignored."""
    assert _generate(520.0, caplog) == []
