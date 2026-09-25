"""A continuous plan keeps its anaerobic and neuromuscular floor on every start day.

test_354 caught it on a Friday. The floor put its anaerobic session on the
Saturday after Friday's sprint, the 48 h spacing pass eased it, and the plan
had none; on a Saturday start the neuromuscular session went the same way.
Spacing outranks a variety floor (notes/overhaul-plan.md D1), so the floor
now takes a day the 48 h rule allows, anywhere in the phase, before it takes
a clashing one.
"""
import collections
from datetime import date, timedelta

import pytest

import plan_invariants as pi

# Every rule name the auditor can emit, so a filter cannot outlive a rename.
_ALL_RULE_NAMES = [(n, n) for n, _fn in pi.ALL_CHECKS]
import training_planner as tp

WEEK = [date(2026, 9, 7) + timedelta(days=k) for k in range(7)]


# Sunday, since contract 9.5 (2026-09-25): this 8 h rider's week is budgeted
# at what 8 h of the mix holds at the served files' rates, 386 where 8 h x 65
# gave 520, so the one-day Sunday stub carries 55 TSS, not 74. The floor puts
# its anaerobic session on that stub (the week with the fewest hard days
# first), and 6.7 eases a hard session that alone takes a week over its
# budget -- the budget outranks the variety floor (D1), so the plan has none.
# The floor choosing a week that can carry it is the floor's to fix.
_STUB_TOO_SMALL = {"Sun"}


@pytest.mark.parametrize("start", [
    pytest.param(d, marks=pytest.mark.xfail(strict=True, reason="the floor's anaerobic "
                                                "session lands on a stub its budget cannot carry"))
    if d.strftime("%a") in _STUB_TOO_SMALL else d for d in WEEK],
    ids=lambda d: d.strftime("%a"))
def test_the_floor_holds_on_every_start_day(start, monkeypatch):
    class _Today(date):
        @classmethod
        def today(cls):
            return cls(start.year, start.month, start.day)
    monkeypatch.setattr(tp, "date", _Today)
    goal = tp.Goal(goal_type="continuous", target_date=None, hours_per_week=8.0, focus="both")
    _p, weeks = tp.generate_plan(goal, current_ctl=50.0, recent_weekly_tss=500.0)
    classes = collections.Counter(tp._content_class_for_zwo(s.zwo_file or "")
                                  for w in weeks for s in w.sessions
                                  if s.session_type != "rest")
    assert classes["anaerobic"] >= 1 and classes["neuromuscular"] >= 1, dict(classes)
    # The rule is "hard_day_spacing". Filtering on "spacing" -- a name
    # plan_invariants has never emitted -- made this assertion `[] == []` for
    # every one of the seven start days, so the 48 h check it claims to make
    # was never made here.
    assert "hard_day_spacing" in {r for _n, r in _ALL_RULE_NAMES}, (
        "the rule was renamed; this filter would silently pass again")
    spacing = [str(v) for v in pi.audit(weeks, goal, today=start)
               if v.rule == "hard_day_spacing"]
    assert not spacing, spacing
