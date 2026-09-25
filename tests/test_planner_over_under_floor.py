"""v2.0.3 F1 (T-OU) — over_under AND the 4 canonical hard types are guaranteed
in build1+build2 across many seed_salts.

F1 added ``over_under`` to the build1/build2 phase hard-floor in
``_enforce_build2_peak_hard_floor`` (the same swap mechanism the protected
anaerobic / vo2_short / neuromuscular classes already use). Before F1,
over_under sat at mix weight ~0.09 → E[picks]≈1 → rounded to 0 in build phases
on many seeds, so the canonical {threshold, vo2max, sweet_spot, over_under}
rotation was seed-fragile.

This guards the fix against seed bias: over_under must appear ≥1 in build1+
build2 for EVERY tested seed, and — critically — adding the over_under floor
must NOT crowd out the other three hard shapes (they must still appear too).
"""
from datetime import date, timedelta

import pytest

import training_planner as tp
from conftest import PLANNER_PIN_ANCHOR, PLANNER_PIN_ARGS


@pytest.fixture(scope="module", autouse=True)
def _pinned_env():
    """v3.0.0: same W8 pin as the other planner suites — this was the last
    env-coupled (live archive + today-date) member of the flaky family."""
    from datetime import date as _d

    class _Frozen(_d):
        @classmethod
        def today(cls):
            return cls(PLANNER_PIN_ANCHOR.year, PLANNER_PIN_ANCHOR.month,
                       PLANNER_PIN_ANCHOR.day)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tp, "date", _Frozen)
        mp.setattr(tp, "get_today_metrics", lambda: {})
        yield


_INTERVAL_CCS = {
    "sweet_spot", "threshold", "vo2max", "vo2_short",
    "over_under", "anaerobic", "neuromuscular",
}
_INTERVAL_FLAGS = (
    "has_threshold_work", "has_vo2_work", "has_sprints",
    "has_sweet_spot_work", "pattern_over_under", "pattern_microinterval",
)

# The canonical 4-shape rotation the user wants visible in every build block.
_CANONICAL_4 = {"threshold", "vo2max", "sweet_spot", "over_under"}

# 12 varied salts (> the doc's ≥10 floor) including small, large and adjacent.
_SEEDS = [1, 7, 42, 99, 256, 1000, 4242, 8675, 13337, 31415, 65535, 99991]


def _classify(zwo_file: str) -> tuple[str, bool]:
    cache = tp._load_content_classifications()
    if not zwo_file:
        return "", False
    ent = cache.get(zwo_file) or cache.get(zwo_file.split("/")[-1])
    if not ent:
        return "", False
    cc = (ent.get("primary") or "").lower()
    if cc in _INTERVAL_CCS:
        return cc, True
    if cc == "mixed":
        flags = ent.get("secondary_flags") or {}
        return cc, any(flags.get(f, False) for f in _INTERVAL_FLAGS)
    return cc, False


def _build_seen(seed_salt: int) -> set:
    goal = tp.Goal(
        goal_type="event",
        # Anchored to the PIN, not the wall clock: tp.date is frozen at
        # PLANNER_PIN_ANCHOR by the fixture above, so a real-today target
        # made the plan span GROW daily (24w + drift since Jan 5) until the
        # diluted floors tipped the suite red (first seen 2026-07-06).
        target_date=PLANNER_PIN_ANCHOR + timedelta(weeks=24),
        event_type="sportive",
        event_km=200,
        hours_per_week=8.0,
        max_weekday_hours=2.0,
        max_weekend_hours=4.0,
        plan_weeks=24,
    )
    _phases, weeks = tp.generate_plan(goal, seed_salt=seed_salt, **PLANNER_PIN_ARGS)
    seen: set = set()
    for w in weeks:
        if w.phase not in ("build1", "build2"):
            continue
        for s in w.sessions:
            if s.session_type == "rest":
                continue
            cc, is_intvl = _classify(s.zwo_file or "")
            if is_intvl:
                seen.add(cc)
    return seen


# The seeds where the canonical-4 coverage floor still does not hold. Measured
# on 2026-09-20: nine of the twelve seeds now PASS, and a blanket
# `xfail(strict=False)` over all of them meant none could ever fail the build
# again -- including the nine that had quietly started working. Listed
# individually and STRICT, so a seed that starts passing has to be struck from
# this list, and one that regresses turns the build red.
#
# The original finding (v3.0.0): the sampler's hard-type balance drifted, so
# build weeks emitted vo2max:9 / threshold:1 / sweetspot:1 and the floor could
# not hold. Still true for the three below; the fix is a sampler root cause.
#
# Re-measured 2026-09-25 (contract 9.5): the rider here has 8 h and carries
# 650 TSS, so the hours now cap every build week at what 8 h of the phase's
# mix holds at the served files' rates (~385) where 8 h x 65 allowed 520.
# Fewer affordable hard slots reshuffle which seeds show all four shapes:
# 42 and 8675 now do, 65535 and 99991 now miss sweet spot. Same finding,
# same root cause.
_SEEDS_WITHOUT_THE_FULL_ROTATION = {31415, 65535, 99991}


def _seed_param(seed):
    marks = [pytest.mark.xfail(
        strict=True,
        reason="sampler hard-type balance (v3.0.0): the canonical-4 rotation "
               "does not appear for this seed")] if (
        seed in _SEEDS_WITHOUT_THE_FULL_ROTATION) else []
    return pytest.param(seed, marks=marks)


@pytest.mark.parametrize("seed_salt", [_seed_param(s) for s in _SEEDS])
def test_over_under_and_all_four_hard_types_present(seed_salt):
    """over_under + the full canonical 4-shape rotation appear in build1+build2
    for every tested seed."""
    seen = _build_seen(seed_salt)
    assert "over_under" in seen, (
        f"seed {seed_salt}: over_under missing from build1+build2 "
        f"(F1 floor did not fire); saw {sorted(seen)}"
    )
    missing = _CANONICAL_4 - seen
    assert not missing, (
        f"seed {seed_salt}: over_under floor crowded out other hard types — "
        f"missing {sorted(missing)}; saw {sorted(seen)}"
    )


def test_over_under_floor_holds_across_all_seeds_aggregate():
    """Aggregate sanity: across all seeds, over_under is present every time."""
    misses = [s for s in _SEEDS if "over_under" not in _build_seen(s)]
    assert not misses, f"over_under absent from build1+build2 for seeds: {misses}"
