"""Continuous-mode daily policy (3.4.0 W2 — IP_CONTINUOUS_MODE amendments C+D).

Pure decision functions for the open-ended ("continuous") goal type:

* ``today_headline`` — today's headline (contract slim/9.3, H1-H3): the
  session the rider is told to ride, by its goal zone on the planner's three
  zones (zones.goal_zone; the adjusted session when the day was adjusted),
  with the HRV-gated policy's rules as cautions. Until 2026-10-12 this was a
  rotation policy that picked a family of its own from the week's zone-rail
  deficits and never read today's session: on a Monday every hard minute of
  the week was "owed today", over a planned recovery ride. The gates (grill
  P3 verdict, GRILL_CONTINUOUS.md):
    - Hard days only when the 7-day LnRMSSD sits INSIDE the SWC band
      (baseline mean ± 0.5 SD) — Javaloyes 2019 IJSPP 14(1):23-32 and
      Javaloyes 2020 (both RCT, cyclists); band adopted TWO-SIDED per the
      Vesterinen/Altini refinement (a spike above the band is also a
      stress signal).
    - TSB deep-fatigue floor mirrors the reforecast downshift line
      (training_planner: ``tsb < TSB_EASE_BELOW``, -30).
    - 48h hard-day spacing + easy day-after a glycolytic session (Hulin
      2014 rolling 48h Z5+ ceiling; the R5 glyco day-after rule).
    - A deload week keeps every ride easy (Issurin 2010).
  Whether a gate should also change the session is the adjustment's job
  (training_planner.adjust_today_session), not the headline's (H2).

* ``deload_trigger`` / ``foster_monotony`` — amendment C values (grill P4):
  advance the scheduled deload when Foster monotony >= 2.0 (Foster 1998
  Med Sci Sports Exerc 30:1164) or weekly ACWR > 1.5 (Gabbett 2016 Br J
  Sports Med 50:273-280, the existing injury-risk line in
  training_planner's G4 gate).

Hermetic by design: stdlib only, no I/O, no repo imports — the app layer
(app.py) feeds it already-computed series and persists any consequence.
"""
from __future__ import annotations

# ── Families: the planner's three zones (zones.goal_zone), and rest ────────
FAMILY_REST = "rest"
FAMILIES = ("zone_1", "zone_2", "zone_3", FAMILY_REST)
ZONE_NAME = {1: "zone 1", 2: "zone 2", 3: "zone 3"}
# Coggan detail, a sub-label only (D1): VO2max is aerobic work in zone 3.
SESSION_LABEL = {
    "recovery": "Recovery", "z2": "Endurance", "long_z2": "Long endurance",
    "endurance": "Endurance", "tempo": "Tempo", "sweetspot": "Sweet spot",
    "threshold": "Threshold", "overunder": "Over-under", "vo2max": "VO2max",
    "sprint": "Sprint", "anaerobic": "Anaerobic capacity", "ftp_test": "FTP test",
    "rest": "Rest",
}

# ── Amendment C thresholds (grill P4) ───────────────────────────────────────
MONOTONY_DELOAD_MIN = 2.0   # Foster 1998: monotony >= 2.0 = elevated risk
ACWR_DELOAD_MIN = 1.5       # Gabbett 2016: > 1.5 doubles injury risk
# JSON-safe stand-in for an infinite monotony (7 identical daily loads ⇒
# SD = 0). Foster monotony is realistically < 7; 99 is unambiguous.
MONOTONY_CAP = 99.0

# ── Headline gates (grill P3) ─────────────────────────────────────────────
TSB_LOW_FLOOR = -30         # training_planner.TSB_EASE_BELOW; a test pins them equal
ANAEROBIC_SPACING_DAYS = 2  # 48h hard-day spacing + glyco day-after (R5)
# A ride counts as glycolytic exposure at >= 60s of Z6+Z7 — the sprint
# contract's t150 >= 60s admissibility bar (grill P6).
ANAEROBIC_Z67_MIN_S = 60
# Minimum gap that counts as "owed" — sub-floor gaps are zone-edge noise,
# not a prescription. Zone 3 minutes are inherently few, zone 2 bulk.
OWED_MIN_MINUTES = {2: 10, 3: 2}


def foster_monotony(daily_tss) -> "float | None":
    """Raw Foster (1998) monotony over a daily-load window: mean / SD.

    ``daily_tss`` is a sequence of per-day loads (typically 7 entries, one
    per calendar day, zeros for rest days). Returns None when the window
    carries no load at all; an all-equal loaded week (SD = 0, the textbook
    worst monotony) returns MONOTONY_CAP so callers/JSON never see inf.
    Population SD (÷ n) — same math as app._monotony_score's banding.
    """
    vals = [float(v or 0) for v in (daily_tss or [])]
    if not vals or sum(vals) <= 0:
        return None
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    sd = var ** 0.5
    if sd <= 0:
        return MONOTONY_CAP
    return min(mean / sd, MONOTONY_CAP)


def deload_trigger(monotony: "float | None",
                   acwr: "float | None") -> "dict | None":
    """Amendment C trip-wire: should the deload be pulled into THIS week?

    Returns None when neither series trips, else a JSON-safe record
    ``{trigger, value, threshold, reason}``. Monotony is evaluated first
    (grill P4 order: "monotony >= 2.0 OR ACWR > 1.5").
    """
    if monotony is not None and monotony >= MONOTONY_DELOAD_MIN:
        # The SD=0 cap value is an internal stand-in — phrase it honestly
        # instead of leaking "monotony 99.00" into the chip.
        mono_txt = (f"training monotony {monotony:.2f} ≥ "
                    f"{MONOTONY_DELOAD_MIN:.1f} over the last 7 days"
                    if monotony < MONOTONY_CAP else
                    "the last 7 days carried near-identical daily load "
                    "(maximal monotony)")
        return {
            "trigger": "monotony",
            "value": round(float(monotony), 2),
            "threshold": MONOTONY_DELOAD_MIN,
            "reason": (
                f"Deload pulled forward — {mono_txt} (Foster 1998: "
                f"monotonous loading raises overtraining risk)."
            ),
        }
    if acwr is not None and acwr > ACWR_DELOAD_MIN:
        return {
            "trigger": "acwr",
            "value": round(float(acwr), 2),
            "threshold": ACWR_DELOAD_MIN,
            "reason": (
                f"Deload pulled forward — last week you absorbed "
                f"{acwr:.2f}× the planned load (ACWR > "
                f"{ACWR_DELOAD_MIN:.1f} doubles injury risk, Gabbett 2016)."
            ),
        }
    return None


def hrv_band(ln_rmssd_7d: "float | None", swc_lower: "float | None",
             swc_upper: "float | None") -> str:
    """Where the 7-day LnRMSSD sits vs the SWC band (mean ± 0.5 SD).

    Returns "in_band" | "below" | "above" | "unknown". Two-sided on
    purpose (Vesterinen/Altini): an HRV spike ABOVE the band is treated as
    a stress signal too, unlike the one-sided 2019 rule.
    """
    if ln_rmssd_7d is None or swc_lower is None or swc_upper is None:
        return "unknown"
    if ln_rmssd_7d < swc_lower:
        return "below"
    if ln_rmssd_7d > swc_upper:
        return "above"
    return "in_band"


def _label(session_type: str) -> str:
    st = (session_type or "").lower()
    return SESSION_LABEL.get(st, st.replace("_", " ").capitalize() or "Session")


def today_headline(
    session_type: str,
    goal_zone: "int | None",
    readiness: "dict | None",
    days_since_last_anaerobic: "int | None",
    deload_week: bool = False,
    owed: "dict | None" = None,
    planned_type: "str | None" = None,
    adjustment_reason: str = "",
) -> dict:
    """Today's headline: the session the rider is told to ride (H1).

    Args:
        session_type: the effective session's content type (the adjusted one
            when the day was adjusted, else the week view's session_type).
        goal_zone: its zone, zones.goal_zone (None: rest or unknown).
        readiness: ``{ln_rmssd_7d, swc_lower, swc_upper, tsb}`` (any may be
            None).
        days_since_last_anaerobic: days since the last ride with >=
            ANAEROBIC_Z67_MIN_S of Z6+Z7 time; None = none on record.
        deload_week: the current plan week is a deload.
        owed: minutes per zone ({"zone2": .., "zone3": ..}) the week still
            owes that no planned day holds (week_view.zone_owed, H3).
        planned_type / adjustment_reason: the planned type and the reason,
            when the adjustment changed the session.

    Returns ``{family, goal_zone, session_type, reason}``; family is
    "zone_N" or "rest". A gate that would forbid a hard day appears as a
    caution when the session is hard; it never names another family (H2).
    """
    st = (session_type or "rest").lower()
    rest = st == "rest"
    hard = goal_zone in (2, 3) and not rest
    family = FAMILY_REST if rest else f"zone_{goal_zone}" if goal_zone else st
    what = "Rest" if rest else _label(st)
    if not rest and goal_zone:
        what += f" ({ZONE_NAME[goal_zone]})"
    if planned_type and planned_type.lower() != st:
        head = f"{what} today, adjusted from {_label(planned_type)}"
        head += f": {adjustment_reason.rstrip('.')}." if adjustment_reason else "."
    else:
        head = f"{what} today, as planned."
    r = readiness or {}
    ln7, lo, hi, tsb = r.get("ln_rmssd_7d"), r.get("swc_lower"), r.get("swc_upper"), r.get("tsb")
    band = hrv_band(ln7, lo, hi)
    notes = []
    if band == "in_band":
        notes.append("HRV in band.")
    elif band == "unknown":
        notes.append("No HRV baseline yet.")
    elif hard:
        where = "below" if band == "below" else "above"
        notes.append(f"Caution: 7-day LnRMSSD {ln7:.2f} is {where} your normal band "
                     f"({lo:.2f}–{hi:.2f}); hard days only when HRV is in band "
                     f"(Javaloyes 2019{', two-sided per Vesterinen/Altini' if where == 'above' else ''}).")
    else:
        notes.append(f"HRV {'below' if band == 'below' else 'above'} your normal band: an easy day fits.")
    if hard and tsb is not None and float(tsb) < TSB_LOW_FLOOR:
        notes.append(f"Caution: TSB {float(tsb):.0f} is under {TSB_LOW_FLOOR} (deep fatigue).")
    dsa = days_since_last_anaerobic
    if hard and dsa is not None and dsa < ANAEROBIC_SPACING_DAYS:
        notes.append(f"Caution: glycolytic work (Z6-Z7) {dsa}d ago; keep 48h between "
                     f"hard days (Hulin 2014).")
    if hard and deload_week:
        notes.append("Caution: deload week, every ride easy (Issurin 2010).")
    # On a rest or recovery day (planned, or the adjustment's) the week's
    # debt is not today's business: nothing owed is said (SF2).
    for z in (2, 3) if st not in ("rest", "recovery") else ():
        v = float((owed or {}).get(f"zone{z}") or 0)
        if v >= OWED_MIN_MINUTES[z]:
            notes.append(f"{ZONE_NAME[z].capitalize()} still owed {v:.0f}min this week "
                         f"beyond the planned days.")
    return {"family": family, "goal_zone": None if rest else goal_zone,
            "session_type": "rest" if rest else st,
            "reason": " ".join([head] + notes)}
