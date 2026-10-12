"""One view of a week, read from the stored plan once and served to every card.

Until 2026-09-14 each reader derived the week for itself: /api/weekly-plan ran
a second planner on every GET and served its target as the home badge, the
rollup and the "Last week" card; the Today card read a session's label while
the This Week cell read its file, and the stored record carried both,
disagreeing; the calendar's history rows said a past week was planned at 0
while the rollup invented a target for it (notes/review/architecture-audit-
2026-09-14.md, notes/week-view-contract.md). This module is the contract's
P1, P3 and P9 on the read side: one boundary, one planned load, one budget,
one identity (the served file), one intensity count (the planner's three
zones, slim/9.3), and "no plan on record" said as
None, never as 0 or as a number nobody prescribed.

The write side of the week (the builders, the passes, what a session's label
means to the sampler) is Step 6 of notes/overhaul-plan.md; the stored label
stays there as the slot's, and is exposed here as ``slot_type``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional

import clock
import zones

# A week within this of its budget holds it: the planner's volume pass trims
# and fills to the same band (training_planner._VOLUME_CEILING_TOLERANCE).
BUDGET_TOLERANCE = 1.05
# The weeks that unload a block (plan_invariants.UNLOAD_PHASES).
UNLOAD_PHASES = ("taper", "recon", "recovery_ramp", "consolidation")

# The planner's three zones (zones.THREE_ZONE_FROM_COGGAN, owner's D1), as
# the payloads name them.
ZONES = ("zone1", "zone2", "zone3")


def goal_zone_of(session: dict, lib: dict, session_type: str) -> "int | None":
    """The session's goal zone (zones.goal_zone): its served file's content
    class first, else its content type."""
    row = lib.get(session.get("zwo_file") or "") or {}
    return zones.goal_zone(row.get("ContentClass"), session_type)


def planned_zone_minutes(session: dict, lib: dict, session_type: str,
                         minutes_of: "Optional[Callable[..., Optional[dict]]]" = None):
    """(minutes per zone, method) of a planned session. The method is "time
    in zone" when the served workout was read (every second of it, no
    goal-zone credit: contract slim/9.3 amendment 2; ``minutes_of(zwo_file,
    slot_min, slot_type)`` reads it), "by type" when no file could be read and
    the whole duration counts to the goal zone (zone 1 when unknown), and None
    for a rest day."""
    out = {z: 0.0 for z in ZONES}
    dur = float(session.get("duration_min") or 0)
    if session_type == "rest" or dur <= 0:
        return out, None
    zf = session.get("zwo_file") or ""
    m = None
    if zf and minutes_of is not None:
        slot = session.get("slot_type") or session.get("session_type") or ""
        m = minutes_of(zf, session.get("duration_min"), slot)
    if m:
        return {f"zone{i}": float(m.get(f"z{i}") or 0) for i in (1, 2, 3)}, "time in zone"
    out[f"zone{goal_zone_of(session, lib, session_type) or 1}"] = dur
    return out, "by type"


def zone_method_label(methods) -> str:
    """The method a total was counted by: the one method when there is one
    ("time in zone", "by type", "hybrid (HR)" ...), else the most common
    followed by the counts of the others ("time in zone, 1 by type")."""
    from collections import Counter
    c = Counter(m for m in methods if m)
    if not c:
        return "time in zone"
    (top, _n), *others = c.most_common()
    return ", ".join([top] + [f"{n} {m}" for m, n in others])


def ride_minutes(power_tiz: "dict | None", hr_tiz: "dict | None",
                 goal: "int | None", hr_zones: "Optional[dict]" = None) -> "Optional[tuple]":
    """(minutes per zone, method) of a ride (contract slim/9.3, amendment 2),
    or None when it carries neither power nor HR time in zone. With power:
    time in zone, every second ("time in zone"). HR only: the hybrid for the
    goal zone of the session its completion match says it is when that goal
    is zone 2 or 3 ("hybrid (HR)", zones.hr_hybrid_minutes), else HR time in
    zone ("HR time in zone"). ``hr_zones`` places the ride's HR zones on the
    three (zones.hr_zone_map), Friel's seven when None."""
    p = zones.three_zone(power_tiz or {})
    if sum(p.values()) > 0:
        return {f"zone{i}": p[f"z{i}"] / 60.0 for i in (1, 2, 3)}, "time in zone"
    if sum(float(v or 0) for k, v in (hr_tiz or {}).items() if str(k).startswith("z")) > 0:
        m = zones.hr_hybrid_minutes(hr_tiz, goal, hr_zones)
        return ({f"zone{i}": m[f"z{i}"] for i in (1, 2, 3)},
                "hybrid (HR)" if goal in (2, 3) else "HR time in zone")
    return None


def zone_owed(sessions, ridden: dict, today: date, rode_today: bool) -> dict:
    """Minutes of zone 2 and zone 3 the week still owes that no planned day
    holds (contract slim/9.3, H3): the hard-zone minutes of the week's planned
    hard sessions (goal zone 2 or 3), minus the zone 2 / zone 3 minutes
    ridden this week up to today (``ridden``), minus what the remaining hard
    sessions hold (the days after today, and today when it has not been
    ridden). Never below 0; zone 1 is never owed. An easy day's surges are
    not a debt: a missed endurance ride with two tempo pickups owes nothing
    (the evaluation of 2382cfd1, SF2). ``sessions`` carry ``zone_minutes``,
    ``goal_zone`` and ``day``. (An adjusted day says nothing owed:
    continuous_policy.today_headline.)"""
    t = today.isoformat()
    hard = ("zone2", "zone3")
    planned = {z: 0.0 for z in hard}
    held = {z: 0.0 for z in hard}
    for s in sessions or []:
        if s.get("goal_zone") not in (2, 3):
            continue
        zm = s.get("zone_minutes") or {}
        d = s.get("day") or ""
        for z in hard:
            v = float(zm.get(z) or 0)
            planned[z] += v
            if d > t:
                held[z] += v
            elif d == t and not rode_today:
                held[z] += v
    out = {z: max(0.0, planned[z] - float(ridden.get(z) or 0) - held[z]) for z in hard}
    return {"zone1": 0.0, **out}


def covered_days(sessions) -> "list[str]":
    """The days in the week the plan actually covers, as ISO strings.

    A plan is not always a calendar week. It can start mid-week (a replan, a
    first plan, an entry backdated from a past date), stop mid-week (the last
    week before an event), or cover a single day. `on_record` is already the
    per-day answer -- a stored row covers that day -- and it is what makes the
    grade comparable: rides are counted over the same days the plan asked for.
    """
    return [s["day"] for s in sessions or []
            if s.get("on_record") and s.get("day")]


def week_done(rides, start: date, end: date, days=None) -> float:
    """What the rider actually rode in the week [start, end], in TSS.

    The third of the week's three numbers, and the last one to get an owner.
    The budget and the prescribed load were already read here once and served
    to every card; this was still summed independently by /api/week-summary
    and by the calendar's own merge. They agreed because both loop over the
    same rides with the same filter, which is agreement by coincidence: a
    change to one was a silent disagreement with the other, which is exactly
    how "499 done against 138 planned" happened one layer up.

    ``rides`` are the ride records ride_storage owns, or the flatter activity
    dicts /api/activities serves; both carry a date and a tss.

    ``days`` narrows it to a set of ISO days -- the overlap with what the plan
    covers (see covered_days). Without it the answer is the whole week, which
    is the right number to SHOW and the wrong one to GRADE against a plan that
    starts on Friday: 499 ridden over seven days against 138 prescribed over
    three is how the rollup read 362% and displayed its 150% cap.
    """
    lo, hi = start.isoformat(), end.isoformat()
    only = set(days) if days is not None else None
    total = 0.0
    # Each ride once, and only rides: the week's "done" is the same number the
    # planner's net budget reads (plan_invariants.rides_once).
    from plan_invariants import rides_once
    for ride in rides_once(rides):
        day = str(ride.get("date") or ride.get("started_at") or "")[:10]
        if not day or not (lo <= day <= hi):
            continue
        if only is not None and day not in only:
            continue
        try:
            total += float(ride.get("tss") or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 1)


def iso_week(today: Optional[date] = None, offset: int = 0) -> tuple[date, date]:
    """Monday..Sunday of the ISO week ``offset`` weeks from today's."""
    t = today or clock.today()
    start = t - timedelta(days=t.weekday()) + timedelta(weeks=offset)
    return start, start + timedelta(days=6)


def library_by_file(rows) -> dict:
    return {r.get("File"): r for r in (rows or []) if r.get("File")}


def derived_type(session: dict, lib: dict, type_of_row: Callable[[dict], str]) -> str:
    """The session's identity: the type its served file's content carries;
    the stored label only when no file is served. Rest is rest, and a long
    ride serving endurance content stays a long ride (the same band and the
    same hardness; the cards colour and name it, and the Today card's
    adjustments treat it, as long)."""
    slot = (session.get("session_type") or "rest").lower()
    if slot == "rest":
        return "rest"
    row = lib.get(session.get("zwo_file") or "")
    if row is None:
        return slot
    try:
        served = type_of_row(row) or slot
    except Exception:  # noqa: BLE001 - a row the classifier cannot read keeps its slot
        return slot
    return "long_z2" if slot == "long_z2" and served == "z2" else served


@dataclass
class WeekView:
    start: date
    end: date
    offset: int
    on_record: bool                       # a stored row covers any day of it
    week_num: int
    phase: str
    is_stepback: bool
    budget: Optional[float]               # the ramp's tss_target, when one row owns the week
    planned_tss: Optional[float]          # what the rider was given: sum of stored sessions
    sessions: list = field(default_factory=list)   # one per day, Monday first
    planned_minutes: int = 0
    # Minutes per zone the week's sessions prescribe (planned_zone_minutes:
    # time in zone of the served files; contract slim/9.3, I2 + amendment 2).
    zone_minutes_planned: dict = field(default_factory=dict)
    zone_method: str = "time in zone"     # zone_method_label of the sessions
    # Contract 9.6: a week planned under its budget by more than the
    # tolerance says so -- {"budget", "planned", "reason"}: "ridden" when the
    # rides so far and what is still planned reach the budget (then
    # "ridden" carries the rides' TSS), "hours" when the rider's weekly hours
    # are full, "unload" for a recovery or taper week (held lighter than its
    # block), else "other" -- so no card has to present the planned sum as
    # the target.
    short_of_budget: Optional[dict] = None
    # Contract 9.11: the 1.3 x guard's lowering of this week's budget, as the
    # stored week records it ({"from", "to", "when", "carried"}), else None.
    budget_lowered: Optional[dict] = None

    def as_weekly_plan(self) -> dict:
        """The shape /api/weekly-plan serves.

        ONE NAME PER QUESTION. A week has three loads and they used to share
        names: `tss_target` meant the prescribed sum here and the ramp's
        budget in /api/plan's stored weeks, while `budget` and `planned_tss`
        rode along beside it. Three names for two questions in one response is
        how a reader ends up dividing one by the other.

          tss_budget    what the ramp allows
          planned_tss   what the week's sessions add up to
          tss_done      what the rider rode (week_view.week_done)
        """
        return {
            "week_num": self.week_num,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "phase": self.phase,
            "tss_budget": self.budget,
            "planned_tss": self.planned_tss,
            "on_record": self.on_record,
            "is_stepback": self.is_stepback,
            "sessions": [dict(s) for s in self.sessions],
            "zone_minutes_planned": {k: int(round(v)) for k, v in self.zone_minutes_planned.items()},
            "zone_method": self.zone_method,
            "short_of_budget": self.short_of_budget,
            "budget_lowered": self.budget_lowered,
        }


def _placeholder(day: date) -> dict:
    return {"day": day.isoformat(), "day_name": day.strftime("%a"), "session_type": "rest",
            "slot_type": "rest", "duration_min": 0, "tss_estimate": 0, "description": "",
            "zwo_file": "", "zwo_name": "", "display_name": "", "zwo_duration_min": 0,
            "zone_dist": None, "score": None, "zwo_tss": None,
            "zone_minutes": {z: 0 for z in ZONES}, "goal_zone": None, "zone_method": None,
            "status": "pending", "on_record": False}


def build(plan: Optional[dict], start: date, lib: dict, type_of_row: Callable[[dict], str],
          naming: Optional[Callable[..., tuple]] = None, offset: int = 0,
          rides=None, minutes_of: Optional[Callable[..., Optional[dict]]] = None) -> WeekView:
    """The week beginning ``start`` (a Monday) as the stored ``plan`` has it.
    ``rides`` (the ride records, as week_done reads them) tell a week whose
    budget the rider's rides already spent from one the plan left short.
    ``naming(zwo_file, stored_session)`` gives a session's display name and
    the minutes its workout lasts as served; ``minutes_of`` reads a served
    workout's minutes per zone (planned_zone_minutes)."""
    end = start + timedelta(days=6)
    s_iso, e_iso = start.isoformat(), end.isoformat()
    # A replaced plan's prescriptions (kept by Generate) are on record too.
    stored = ((plan or {}).get("replaced_weeks") or []) + ((plan or {}).get("weeks") or [])
    rows = [w for w in stored
            if (w.get("start") or "") <= e_iso and (w.get("end") or "") >= s_iso]
    by_day: dict[str, dict] = {}
    for w in rows:
        for s in w.get("sessions") or []:
            d = s.get("day") or ""
            if s_iso <= d <= e_iso:
                by_day[d] = s
    on_record = bool(rows)
    sessions = []
    planned = 0.0
    minutes = 0
    per_zone = {z: 0.0 for z in ZONES}
    methods: list = []
    for i in range(7):
        day = start + timedelta(days=i)
        stored = by_day.get(day.isoformat())
        if stored is None:
            sessions.append(_placeholder(day))
            continue
        out = dict(stored)
        out.setdefault("day_name", day.strftime("%a"))
        out["slot_type"] = stored.get("session_type") or "rest"
        st = derived_type(stored, lib, type_of_row)
        out["session_type"] = st
        out["on_record"] = True
        zf = stored.get("zwo_file") or ""
        row = lib.get(zf) if zf else None
        if naming is not None:
            try:
                dn, zdur = naming(zf, stored)
                out["display_name"], out["zwo_duration_min"] = dn, zdur
            except Exception:  # noqa: BLE001
                pass
        if row:
            out["zone_dist"] = {f"z{i}": row.get(f"Z{i}%", 0) for i in range(1, 7)}
            out["score"] = row.get("Score")
            out["protocol"] = row.get("Protocol")
            out["zwo_tss"] = row.get("TSS")
        else:
            out.setdefault("zone_dist", None)
            out.setdefault("score", None)
            out.setdefault("zwo_tss", None)
        out["goal_zone"] = goal_zone_of(stored, lib, st)
        zm, out["zone_method"] = planned_zone_minutes(stored, lib, st, minutes_of)
        methods.append(out["zone_method"])
        out["zone_minutes"] = {k: int(round(v)) for k, v in zm.items()}
        for b, v in zm.items():
            per_zone[b] += v
        if st != "rest":
            planned += float(stored.get("tss_estimate") or 0)
            minutes += int(round(float(stored.get("duration_min") or 0)))
        sessions.append(out)
    owner = None
    # The current plan's rows own the week; a replaced plan's only when it has
    # no row here (its budget is not the current one).
    owners = [w for w in rows if not w.get("carried_from_generate")] or rows
    if owners:
        # The row that owns the week: the one covering Monday, else the first.
        owner = next((w for w in owners if (w.get("start") or "") <= s_iso), owners[0])
    budget = None
    if owner is not None and len(owners) == 1 and owner.get("tss_target") not in (None, ""):
        budget = float(owner["tss_target"])
    short = None
    if on_record and budget and planned * BUDGET_TOLERANCE < budget:
        hpw = float(((plan or {}).get("goal") or {}).get("hours_per_week") or 0)
        days = sum(1 for s in sessions if s.get("on_record"))
        phase = str((owner or {}).get("phase") or "").lower()
        # What the rider rode this week, and what the plan still asks after
        # today: when those reach the budget, the planner held the rest of
        # the week back because the rides spent it (the owner's 09-21 after
        # the 09-25 sync: 434 planned of 479, 503 ridden, Friday rested by
        # the budget) -- the evaluation of a73647b3, S3.
        today = clock.today()
        ridden = week_done(rides, start, min(end, today)) if rides is not None else 0.0
        rode_today = rides is not None and week_done(rides, today, today) > 0
        first = (today + timedelta(days=1) if rode_today else today).isoformat()
        ahead = sum(float(x.get("tss_estimate") or 0) for x in sessions
                    if x.get("on_record") and x.get("session_type") != "rest"
                    and (x.get("status") or "pending") == "pending"
                    and (x.get("day") or "") >= first)
        if ridden > 0 and (ridden + ahead) * BUDGET_TOLERANCE >= budget:
            reason = "ridden"
        elif hpw > 0 and minutes >= hpw * 60 * days / 7 - 15:
            reason = "hours"
        elif (owner or {}).get("is_stepback") or phase in UNLOAD_PHASES:
            reason = "unload"           # held under its block's lightest week
        else:
            reason = "other"
        short = {"budget": budget, "planned": round(planned, 1), "reason": reason}
        if reason == "ridden":
            short["ridden"] = round(ridden, 1)
    return WeekView(
        start=start, end=end, offset=offset, on_record=on_record,
        week_num=int((owner or {}).get("week_num") or 0),
        phase=str((owner or {}).get("phase") or ""),
        is_stepback=bool((owner or {}).get("is_stepback")),
        budget=budget,
        planned_tss=planned if on_record else None,
        sessions=sessions, planned_minutes=minutes, zone_minutes_planned=per_zone,
        zone_method=zone_method_label(methods),
        short_of_budget=short,
        budget_lowered=(dict(owner["guard_lowered"]) if owner is not None and len(owners) == 1
                        and isinstance(owner.get("guard_lowered"), dict) else None),
    )
