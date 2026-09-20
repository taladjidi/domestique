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
  * two static tests. tests/test_fact_ownership.py checks the rows are
    internally honest: the accessor exists, no two facts share a key name in
    one scope, and nobody assigns a name `config` resolves through the
    profile. tests/test_store_ownership.py measures WHO TOUCHES EACH STORE and
    pins the answer, so a new module reaching past an owner fails, and a
    module that stops reaching has to be struck from the census.

That second test did not exist for a while, and this header claimed it did.
In the meantime `ctl` named `fitness` as its owner while five other modules
wrote the wellness table, and the week facts named `week_view` as the owner of
a file week_view never opens. Both were found by measuring rather than by
reading this paragraph, which is the reason the measurement is now a test.

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
                 "derived", "note")

    def __init__(self, name, owner, store, accessor, api_keys, scope,
                 unit="", tol=0.5, lo=None, hi=None, field="",
                 source_field="", tiers=None, derived=False, note=""):
        self.name, self.owner, self.store = name, owner, store
        self.field = field or name          # the key the store itself uses
        self.accessor, self.api_keys, self.scope = accessor, tuple(api_keys), scope
        self.unit, self.tol, self.lo, self.hi = unit, tol, lo, hi
        self.source_field, self.tiers = source_field, tiers or {}
        # A DERIVED fact is computed by its owner from a store another module
        # keeps: week_view answers what a week asks for, and never opens the
        # plan file. Saying so is the difference between an owner and a
        # fiction -- `owner=week_view` on a store week_view never touches read
        # as ownership until the census measured it.
        self.derived, self.note = derived, note

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
                 api_keys=("pmax_w", "pmax")),
    "max_hr": Fact("max_hr", owner="profile_manager", store="profile:athlete.json",
                   accessor="ProfileManager.max_hr", scope="athlete", unit="bpm",
                   lo=140, hi=220, source_field="max_hr_source",
                   tiers={"manual": 3, "icu": 2, "computed": 1, "age_tanaka": 0},
                   api_keys=("max_hr",)),

    "eftp": Fact(
        "eftp", owner="ride_storage", store="profile:wellness/",
        accessor="ride_storage.latest_eftp", scope="athlete", unit="W", tol=0.5,
        api_keys=("eftp",),
        note="intervals.icu's estimate of the rider's FTP, which is NOT the "
             "FTP they train at (that is `ftp`, which the rider owns). Three "
             "doors serve it -- live ICU, the wellness archive, the profile -- "
             "and /api/settings served blank beside /api/rider-stats' 250."),
    "lthr": Fact(
        "lthr", owner="profile_manager", store="profile:athlete.json",
        accessor="ProfileManager.lthr", scope="athlete", unit="bpm", tol=0.5,
        api_keys=("lthr",)),
    "wprime_kj": Fact(
        "wprime_kj", owner="profile_manager", store="profile:athlete.json",
        accessor="ProfileManager.wprime_j", scope="athlete", unit="kJ", tol=0.1,
        api_keys=("wprime_kj",),
        note="The same fact as `wprime` in a different unit, so it needs its "
             "own row: comparing 20.6 with 20619 would be a false alarm every "
             "time. Two doors serving kJ must still agree with each other."),

    # ── a route ──────────────────────────────────────────────────────────
    # Three stores hold a route's shape: routes.json, the .crs files, and
    # gpx_sources/. /api/routes and /api/courses are two screens for one climb.
    "route_distance_km": Fact(
        "route_distance_km", owner="routes_lib", store="routes:routes.json",
        accessor="routes_lib._load_routes_v2", scope="route", unit="km", tol=0.15,
        api_keys=("distance_km",)),
    "route_climb_m": Fact(
        "route_climb_m", owner="routes_lib", store="routes:routes.json",
        accessor="routes_lib._load_routes_v2", scope="route", unit="m", tol=5.0,
        api_keys=("climb_m", "elevation_gain_m"),
        note="Measured across the library: the two doors disagree on dozens "
             "of routes, by up to a kilometre of climbing."),

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
        "week_budget", owner="week_view", derived=True, store="plan:current_plan.json",
        accessor="week_view.WeekView.budget", scope="week", tol=0.5,
        api_keys=("tss_budget", "weekly_tss_target", "target_tss"),
        note="What the ramp allows this week. The stored plan calls this "
             "`tss_target`; the responses call it tss_budget, because every "
             "other endpoint used `tss_target` for the prescribed sum."),
    "week_prescribed": Fact(
        "week_prescribed", owner="week_view", derived=True, store="plan:current_plan.json",
        accessor="week_view.WeekView.planned_tss", scope="week", tol=0.5,
        api_keys=("planned_tss",),
        note="What the week's sessions actually add up to. The rollup divides "
             "the rides by THIS."),
    "week_done_in_plan": Fact(
        "week_done_in_plan", owner="week_view", derived=True, store="profile:rides/",
        accessor="week_view.week_done", scope="week", tol=0.5,
        api_keys=("tss_done_in_plan",),
        note="What the rider rode on the days the plan actually covers. The "
             "grade is this against week_prescribed, never the week's total "
             "against a plan that covers three days of it: that is the "
             "reported 499/138, where intervals.icu said 499/387."),
    "week_done": Fact(
        "week_done", owner="week_view", derived=True, store="profile:rides/",
        accessor="week_view.week_done", scope="week", tol=0.5,
        api_keys=("tss_done", "actual_tss", "tss_actual"),
        note="What the rider rode in the calendar week. A consumer of the ride "
             "facts, not a store of its own: the rollup and the calendar each "
             "summed it and agreed by coincidence."),

    # ── one planned session ──────────────────────────────────────────────
    "session_planned_tss": Fact(
        "session_planned_tss", owner="week_view", derived=True, store="plan:current_plan.json",
        accessor="week_view.WeekView.sessions", scope="session", tol=0.5,
        api_keys=("tss_estimate",)),
    "session_planned_min": Fact(
        "session_planned_min", owner="week_view", derived=True, store="plan:current_plan.json",
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
