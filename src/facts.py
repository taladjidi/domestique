"""Who owns each number, where it lives, and what it is called.

This is the registry the rest of the app is measured against. Every expensive
bug in this project has been the same shape: a number with an owner on paper
and a second writer, or a second reader, in practice. CTL merged per-field
from intervals.icu and a local EWMA. A ride's duration meaning elapsed in one
screen and moving in another. A week's load meaning three different things
under one key name. An FTP proxy switched off by a single assignment.

None of those was a wrong formula. Each was a missing owner.

So the ownership is written down once, here, and three things read it:

  * the code, for the facts whose writer gate is table-driven
    (profile_manager._set_fact);
  * the runtime probe (tools/arch/quantities.py), which asks every endpoint
    the same question and fails when one question has two answers -- it builds
    its alias table from `api_keys` below rather than keeping a second copy;
  * the static ownership test (tests/test_fact_ownership.py), which fails when
    a module that is not the owner touches the store.

A fact added here gets all three for free. A fact that disagrees with the code
fails a test rather than a rider.

WHAT A ROW MEANS
  owner      the module allowed to write the store, and the only one that
             should read it directly. Everything else calls `accessor`.
  store      where the value actually lives.
  accessor   the one function every reader is meant to call.
  api_keys   every key name the value travels under in a JSON response. Two
             different quantities must never share a name here; that is the
             bug the week-load rows record.
  scope      what the number is ABOUT, so two readings are only compared when
             they describe the same thing: the athlete on a day, one ride, one
             week, one planned session.
  tol        how far apart two readings may be before it is a disagreement
             (independent rounding, not a licence to differ).
"""
from __future__ import annotations


class Fact:
    """One number, one owner."""

    __slots__ = ("name", "owner", "store", "accessor", "api_keys", "scope",
                 "unit", "tol", "lo", "hi", "field", "source_field", "tiers",
                 "note")

    def __init__(self, name, owner, store, accessor, api_keys, scope,
                 unit="", tol=0.5, lo=None, hi=None, field="",
                 source_field="", tiers=None, note=""):
        self.name, self.owner, self.store = name, owner, store
        self.field = field or name          # the key the store itself uses
        self.accessor, self.api_keys, self.scope = accessor, tuple(api_keys), scope
        self.unit, self.tol, self.lo, self.hi = unit, tol, lo, hi
        self.source_field, self.tiers, self.note = source_field, tiers or {}, note

    @property
    def tracked_provenance(self) -> bool:
        """True when the fact records where its current value came from, and
        a lower-priority writer is refused."""
        return bool(self.tiers)

    def __repr__(self) -> str:
        return f"<Fact {self.name} owner={self.owner}>"


# Provenance tiers, higher wins; equal tiers may overwrite, so a rider can
# always overrule themselves and a fresh machine reading replaces the last.
_RIDER_OVER_MACHINE = {
    "manual": 2, "tested_coggan_20min": 2, "tested_ramp": 2,
    "eftp_icu": 1, "eftp_auto": 1, "eftp_local": 1,
}

FACTS: "dict[str, Fact]" = {

    # ── the athlete's fitness ────────────────────────────────────────────
    "ctl": Fact(
        "ctl", owner="fitness", store="sqlite:wellness",
        accessor="fitness.state", scope="athlete", unit="", tol=0.55,
        api_keys=("ctl", "ctl_actual", "current_ctl"),
        note="intervals.icu live, then its last values as stored, then the "
             "local archive for a rider with no ICU connection, then unknown. "
             "Readers take the ladder through fitness.state, never their own."),
    "atl": Fact("atl", owner="fitness", store="sqlite:wellness",
                accessor="fitness.state", scope="athlete", tol=0.55,
                api_keys=("atl",)),
    "tsb": Fact("tsb", owner="fitness", store="sqlite:wellness",
                accessor="fitness.state", scope="athlete", tol=0.55,
                api_keys=("tsb",),
                note="ctl - atl, never stored and never recomputed elsewhere. "
                     "The readiness composite scores an input 0..100 and calls "
                     "it tsb as well; that is a different number."),

    # ── the athlete's numbers ────────────────────────────────────────────
    "ftp": Fact(
        "ftp", owner="profile_manager", store="profile:athlete.json",
        accessor="ProfileManager.ftp", scope="athlete", unit="W", tol=0.5,
        lo=50, hi=600, source_field="ftp_source", tiers=_RIDER_OVER_MACHINE,
        api_keys=("ftp", "current_ftp"),
        note="config.ATHLETE_FTP_W proxies to this through config.__getattr__. "
             "Assigning that name makes a real module attribute, which shadows "
             "the proxy for the life of the process."),
    "wprime": Fact("wprime", owner="profile_manager", store="profile:athlete.json",
                   accessor="ProfileManager.wprime_j", scope="athlete", unit="J",
                   lo=5000, hi=40000, field="wprime_j", source_field="wprime_source",
                   tiers={"manual": 3, "icu": 2, "monod": 1, "fallback": 0},
                   api_keys=("wprime_j",)),
    "pmax": Fact("pmax", owner="profile_manager", store="profile:athlete.json",
                 accessor="ProfileManager.pmax_w", scope="athlete", unit="W",
                 lo=300, hi=2500, field="pmax_w", source_field="pmax_source",
                 tiers={"manual": 3, "icu": 2, "computed": 1, "fallback": 0},
                 api_keys=("pmax_w",)),
    "max_hr": Fact("max_hr", owner="profile_manager", store="profile:athlete.json",
                   accessor="ProfileManager.max_hr", scope="athlete", unit="bpm",
                   lo=140, hi=220, source_field="max_hr_source",
                   tiers={"manual": 3, "icu": 2, "computed": 1, "age_tanaka": 0},
                   api_keys=("max_hr",)),

    # ── a ride ───────────────────────────────────────────────────────────
    "ride_duration_min": Fact(
        "ride_duration_min", owner="ride_storage", store="profile:rides/",
        accessor="ride_storage.ride_moving_s", scope="ride", unit="min",
        tol=0.6, api_keys=("duration_min",),
        note="MOVING time, as intervals.icu shows it. duration_s (elapsed) and "
             "moving_s are raw fields, not this fact: one ride read 118 minutes "
             "and 91 on the same card while both numbers were correct."),
    "ride_tss": Fact("ride_tss", owner="ride_storage", store="profile:rides/",
                     accessor="ride_storage.load_all_rides", scope="ride",
                     tol=0.5, api_keys=("tss",)),

    # ── a week of the plan ───────────────────────────────────────────────
    # Three quantities that have been sharing names. Until they have one name
    # each, `tss_target` means the budget in /api/plan and the prescribed sum
    # everywhere else, and the rollup divides one by the other.
    "week_budget": Fact(
        "week_budget", owner="week_view", store="plan:current_plan.json",
        accessor="week_view.WeekView.budget", scope="week", tol=0.5,
        api_keys=("budget", "weekly_tss_target", "target_tss"),
        note="What the ramp allows this week. week_view already keeps this "
             "apart from what the sessions add up to; the API does not, which "
             "is the open half of this row."),
    "week_prescribed": Fact(
        "week_prescribed", owner="week_view", store="plan:current_plan.json",
        accessor="week_view.WeekView.planned_tss", scope="week", tol=0.5,
        api_keys=("tss_target", "planned_tss"),
        note="What the week's sessions actually add up to. The rollup divides "
             "the rides by THIS. /api/plan serves the STORED tss_target, which "
             "is the budget, under the same name -- 500 against 447 on the "
             "probe's fixture. Fixing it means one name per quantity in the "
             "responses, and the dashboard reading the new one."),
    "week_done": Fact(
        "week_done", owner="", store="profile:rides/",
        accessor="", scope="week", tol=0.5,
        api_keys=("tss_done", "actual_tss", "tss_actual"),
        note="NO OWNER YET. Summed independently by /api/week-summary and by "
             "the calendar's own merge. Both sum rides over the calendar week, "
             "so they agree today; nothing makes them."),

    # ── one planned session ──────────────────────────────────────────────
    "session_planned_tss": Fact(
        "session_planned_tss", owner="week_view", store="plan:current_plan.json",
        accessor="week_view.WeekView.sessions", scope="session", tol=0.5,
        api_keys=("tss_estimate",)),
    "session_planned_min": Fact(
        "session_planned_min", owner="week_view", store="plan:current_plan.json",
        accessor="week_view.WeekView.sessions", scope="session", tol=0.5,
        unit="min", api_keys=("duration_min", "planned_min")),
}


def by_store(store: str) -> "list[Fact]":
    return [f for f in FACTS.values() if f.store == store]


def owners() -> "dict[str, str]":
    """store -> the one module allowed to write it."""
    return {f.store: f.owner for f in FACTS.values()}


def provenance_tiers(name: str) -> dict:
    return dict(FACTS[name].tiers)
