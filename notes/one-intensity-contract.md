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

D1. **One intensity model, physiological.** Z1-Z2 low aerobic, Z3 tempo
    (mid), Z4-Z5 high aerobic, Z6-Z7 anaerobic. A session counts by its
    effect: VO2max is high aerobic though it holds Z6 bursts. "The zoned
    model gives more details but should not override."
D2. **Missing wellness is unknown.** Hard days stay as planned; "let's not
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
stored session today, `continuous_suggestion.family` is that session's family
in the one model (D1): the adjusted session when `was_modified`, else the
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
the one model. On the owner's Monday (39 min Z3-Z5 on Tue/Thu) nothing is
owed today.

### Intensity (D1)

**I1 One model, one module.** A single module owns: the four bands, the
type/content-class -> family table, the zone -> band fallback table, and the
split of a session's (or a matched ride's) minutes: Z1-Z2 minutes stay low,
every minute above Z2 counts to the session's family. Every classifier listed
below reads it; no other table maps a session type or a zone to a band.

**I2 Every surface counts the same.** For any week: the week view's planned
exposure, the calendar's planned rails, the headline's owed minutes and the
home exposure tile give the same minutes per band (planned side), and the
actual side counts a ride matched to a session by that session's family, an
unmatched ride by the zone table. The probe's R8 is 0 on the owner's data.

**I3 Decisions.** Completion matching reads the session's family from the
one model; its before/after effect on the owner's ride history is listed in
the evaluation. Nothing in the planner's prescriptions changes: the
characterization golden is unchanged (phase B is a later round).

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

**I4 Family by content first.** The one model's family of a session is read
from its served file's content class first, then its session type: the
planner types an anaerobic file as vo2max (tp ~6450), and that file is
anaerobic in effect.

## Out of scope
The Garmin API route (later, owner). Whether the continuous gates should
change the session (H2). Phase B (above).

## Process
Implementer commits per item with a test that fails without it (mutation in
the message); full gate (`UV_OFFLINE=1`, `unshare -rn`) and characterization
with every golden change read before `--bless`; then an independent evaluator
on the sandbox copy (consistency probe R1-R8 plus readiness with wellness
removed); then the owner's deploy decision.
