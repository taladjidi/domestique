# One answer per fact: intensity, readiness, today's headline (slim/9.3, 2026-10-12)

Branch `slim/9.3-one-intensity` off production 574d844b. Owner's report: the
dashboard's headline said "high aerobic" today over a planned recovery session.

## What was measured (read-only, owner's data, 2026-10-12)

`~/Documents/dq-audit-probes/consistency/run.sh <tree> <name>`: one fact,
every surface, on a sandbox copy of this morning's data. Instrument checked:
its minutes rule fires on 8a555402's six wrong days, its slot-length and TSS
rules on 433c6067's, and all three are quiet on 574d844b. On 574d844b:

- R5 headline vs session: 7 of 7 checked training days disagree, rest days
  too ("Anaerobic today" on Sat/Sun). The headline (`continuous_suggestion`)
  never reads today's session; it subtracts this week's ridden Z3-Z5 minutes
  from the week's planned ones, so on Monday every hard minute of the week is
  "owed today".
- R8 the same week, three intensity counts: week view low/mid+high/anaerobic
  518/1/38 min, calendar Z1Z2/Z3Z4/Z5+ 496/14/48, headline "Z3-Z5 owed 39".
  Inventory: see "Intensity: every classifier" below.
- Readiness: with no wellness (sandbox, or an ICU outage, or the Garmin
  block) `/api/today-session` sets readiness to 50 (`app.py` 10247, plus
  `_readiness_with_data_status`) and `adjust_today_session` reads
  `score or 50` (`training_planner.py` 16943), whose 40-59 rule turns every
  hard session into Z2 ("Readiness 50 (40-59) -> all sessions Z2 or
  easier"). The workout picker invents 70 (`app.py` ~6250).
  `/api/readiness` falls back to the local wellness store; today-session
  does not, so the gauge and the adjustment can read different data.

## Owner's decisions (2026-10-12)

D1. **The planner's convention is the one intensity scale.** Seiler three
    zones anchored on LT1/LT2, FTP as the LT2 proxy, already owned by
    `zones.THREE_ZONE_FROM_COGGAN`: zone 1 = Coggan Z1-Z2 (< 76% FTP), zone 2 =
    Coggan Z3-Z4 (76-105%: tempo, sweet spot, threshold), zone 3 = Coggan
    Z5-Z7 (>= 106%: VO2max, anaerobic capacity, neuromuscular). Coggan levels
    are detail only. VO2max work is aerobic and is never labelled
    "anaerobic". Checked against the literature twice (REPORT-1, an
    independent REPORT-2-check; ~/Documents/dq-audit-probes/intensity-science/
    SYNTHESIS.md). An earlier same-day four-band proposal is withdrawn.
D2. **Counting: the hybrid session-goal / time-in-zone method** (Sylta,
    Tonnessen, Seiler 2014, IJSPP 9:100, doi:10.1123/ijspp.2013-0298,
    recommended there "to standardize TID"): every minute of a session or ride
    is counted; work bouts count to the session's goal zone; warm-up,
    cool-down and active recovery count to zone 1; passive recovery (power 0)
    is not counted. Owner: "it ends up counting the whole ride time in zones,
    which in the end is the natural approach".
D3. **Missing wellness is unknown.** Hard days stay as planned; "let's not
    make assumptions based on missing data". Gates with real data (TSB,
    soreness/Hooper, DFA, 48 h spacing) still apply.

## Assertions

### Readiness

**R1 Unknown is None everywhere it decides.** No code path turns a missing
readiness into a number that a rule reads: `adjust_today_session` skips the
score rules (< 40 rest, < 60 downgrade) when the score is None; the HRV-streak
gates fire only on real HRV; the picker states "readiness unknown" and offers
the planned family instead of a 70 baseline. With every wellness field
missing, a planned VO2max/threshold day is served as planned unless a gate
with data (TSB < -30, soreness >= 6, Hooper, DFA, 48 h) says otherwise.

**R2 Unknown is said, not hidden.** `/api/today-session` and
`/api/readiness` return `score: null` and `data_status` naming what is
missing when readiness cannot be computed; the today card says "readiness
unknown" (no "Readiness 50"), and no adjustment reason quotes a score that
was not computed. `score_0_10`/`score_0_100` are null then.

**R3 One readiness, one source.** `/api/today-session` and `/api/readiness`
compute readiness through one function with the same inputs, including the
local-wellness fallback. Same day, same data: same score, same status, same
missing list.

### Today's headline

**H1 The headline follows the session the rider is told to ride.** With a
stored session today, the headline names that session and its goal zone
(D1, I1): the adjusted session when `was_modified`, else the
planned one, by its content type (week view's `session_type`). A rest day says
rest. The reason names the session ("Recovery today, as planned. HRV in
band."), never a different family.

**H2 Gates explain, they do not contradict.** A continuous-policy gate that
would forbid a hard family (HRV band, TSB floor, 48 h after anaerobic, deload
week) on a day the effective session is hard appears as a caution in the
reason, not as another family. Whether such a gate should also change the
session is out of scope (it is the adjustment's job; one owner).

**H3 Owed minutes count what the plan has not placed.** Where the reason
cites minutes still owed, they are the week's planned minutes of that family
minus those ridden minus those still held by the remaining planned days, in
the hybrid (I1). On the owner's Monday (its intensity on Tue/Thu) nothing is
owed today.

### Intensity (D1, D2)

**I0 Measure before switching.** Before any surface changes, a committed
probe reports, for every library file and for every session of the owner's
plan: minutes per zone by time in zone (the planner's targets) and by the
hybrid, and the per-week difference. The planner's targets stay on time in
zone this round; the evaluation lists the gap between them and the hybrid
displays (phase B input).

**I1 One owner.** `zones.py` owns the scale (already) and gains the one
hybrid counter:
- `goal_zone(content_class, session_type)`: the session's goal zone, content
  class first (the planner types anaerobic files vo2max, tp ~6450): recovery,
  z2, long_z2, endurance -> 1; tempo, sweet spot, threshold, over-under -> 2;
  VO2max, anaerobic, sprint, neuromuscular, FTP test -> 3 (a maximal 20-min or
  ramp test is above CP); unknown -> None.
- `hybrid_minutes(workout, goal_zone)` for a planned workout (the served
  bytes): Warmup, Cooldown, and steady blocks below 76% FTP -> zone 1;
  IntervalsT off-bouts -> zone 1 (active) or uncounted (power 0); every
  other block at or above 76% FTP (a work bout) -> the goal zone. With goal
  zone 1 or None the file is counted by time in zone (an easy ride's
  surges are not reclassified).
- `hybrid_ride_minutes(tiz, goal_zone)` for a ride: matched to a planned
  session, its time below 76% FTP is zone 1 and its time at or above counts
  to the session's goal zone; unmatched, plain time in zone.
No other table maps a session type, content class or Coggan zone to an
intensity bucket for display or advice.

**I2 Every surface counts the same.** The week view's planned and actual
exposure, the calendar's rails and pills, the overreach callout, the
programme summary, the realized distribution readout and the headline's owed
minutes read I1 and use the three zones' names ("zone 1 / 2 / 3", with
Coggan detail such as "VO2max" only as a sub-label). For any week the planned
minutes per zone agree across those surfaces (probe R8 = 0). Payload fields
named after Coggan groupings (`planned_z3z4_min`, `z5plus`) are renamed to
zone names; the dashboard follows. Every distribution figure says its method
("hybrid" or "time in zone").

**I3 Kept as they are, labelled.** The per-ride polarization card (Treff PI,
the number intervals.icu reports) stays time in zone and says so. Completion
matching (`classify_rematch` if_band_ok) keeps its current behaviour exactly,
on its own frozen table; the effect of reading the goal zone instead is
measured and listed (phase B). The planner's prescriptions are unchanged:
characterization golden unchanged.

## Intensity: every classifier (inventory 2026-10-12, 22 places)

Display / advice (this round, phase A):
- week_view.ZONE_BAND / TYPE_BAND / zone_minutes (Z5 -> anaerobic; vo2max,
  overunder -> anaerobic) and app._exposure_split_from_tiz (inline copy);
  the overreach tile (app ~9465, dashboard ~8001).
- app._continuous_family_deficits + continuous_policy families (already Z3-Z5
  high; its focus=vo2 rung picks the ANAEROBIC family).
- calendar: app._planned_zone_split_minutes (vo2max heuristic 55/10/35),
  _ride_zone_split_minutes, the z1z2/z3z4/z5plus rails and pills ("Z5+"
  labelled "Anaerobic").
- programme summary intensity_dist (app ~19983: z1z2/z3/z4plus, Z4 lumped
  with Z5-7).
- realized_band_distribution / _SESSION_TYPE_TO_BAND (5 bands; anaerobic in
  "vo2", overunder in threshold) and the dashboard copy
  TEMPLATE_TYPICAL_BANDS/_BAND_LABELS.
- dashboard maps: EXPOSURE_META, BAND_SHORT, FAM, calContentMatches (copy of
  app accepted_by_type), CAL_CONTENT_LABEL/CSS.
- the polarization card (analytics, zones.THREE_ZONE_FROM_COGGAN) is
  Seiler's three-zone TID; it stays a labelled TID view (see phase B).

Decisions:
- classify_rematch if_band_ok (tp ~16627): the session's band vs the ride's
  IF band (0.65/0.82/0.97). A VO2 session is "anaerobic" today, so its IF
  axis almost never passes. Phase A: the session's family from the one model,
  the ride's IF band unchanged; effect measured on the owner's history and
  listed before acceptance.
- Phase B (NOT this round, owner's call, changes plan content): the planner's
  budgets and sampler (IntensityBudget z1z2/z3/z4/z5plus, PHASE_TID_DOSE,
  polarized_targets 78/6/16, _BAND_TSS_PER_HOUR, _BAND_CLASSES, week_solver,
  workout_gen), the G3 polarization breach (zone-based actual), custom-band
  weights (_BAND_TO_HIT_TYPE), _CONTENT_CLASS_TO_SESSION_TYPE (anaerobic files
  typed vo2max), and the hard/easy sets (tp vs plan_invariants disagree on
  tempo).

## Out of scope
The Garmin API route (later, owner). Whether the continuous gates should
change the session (H2). Phase B (above).

## Process
Implementer commits per item with a test that fails without it (mutation in
the message); full gate (`UV_OFFLINE=1`, `unshare -rn`) and characterization
with every golden change read before `--bless`; then an independent evaluator
on the sandbox copy (consistency probe R1-R8 plus readiness with wellness
removed); then the owner's deploy decision.
