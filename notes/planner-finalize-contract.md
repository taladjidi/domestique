# Planner final-step contract — audit of 2026-09-23, cause 6

Branch `slim/8-planner-finalize`, base `slim/7-hardening` (02a0f514, in
production since 2026-09-24). Findings and repro recipes:
`~/Documents/domestique/notes/review/audit-2026-09-23.md`, "Planner
correctness". The reviewer's probes are kept in `~/Documents/dq-planner-probes/`
(sweep_gen.py, sweep_entries.py, app_avail.py, app_regen.py, app_brace.py,
regen_keep.py, regen_one.py, runway.py, extend_q.py, stages.py, trace.py,
checks.py, negctl.py …; written against 4ad5b95d — re-point paths).

Owner's scope (2026-09-24): "fix the planner items" = cause 6 below. The
overhaul plan's decisions D1–D8 (`notes/overhaul-plan.md`) stand and are not
relitigated. OUT of scope, because each changes what a rider is prescribed and
is the upstream owner's call: 3-day riders' all-hard weeks (H5), capacity_cap
sub-FTP reps (M5), enforcing ACWR (M7 — auditing it is IN, see 6.10), the
today-session gates using the one hardness predicate.

## The structure (the root cause, not the instances)

The reviewer's shared causes: (a) every entry point assembles its own tail, so
availability, file matching, race shaping and the volume ceiling are applied by
some builders and not others; (b) no one owns a week's NET budget; (c) past
days are not treated as a record. The fix is ONE final step that every entry
point runs as its last act on the weeks — generate, regenerate (incl. the
recovery ramp and the sync auto-regen), recalculate, reforecast /
reforecast_dict, extend_continuous_plan, refit, save-availability,
add-race, mark-unavailable, auto-recalc — and that holds the rules below. It
does not re-plan: it enforces. A rule it enforces is deleted from wherever an
entry point used to apply it itself (one implementation, one owner). This is
compatible with overhaul Step 6 (TrainingWeek), which it does not replace.

Done = for each assertion: a test asserts it and passes; the test FAILS with
the fix reverted (mutation + counts in the commit message); the reviewer's
original probe that found it reproduces on 02a0f514 and not on the branch.
The characterization golden WILL move: re-bless only after reading every
changed case, and list in the commit which cases moved and why each move is
one of the assertions below. Full gate: no new failure vs the same-day
02a0f514 baseline.

## Assertions

- **6.1 Availability is honoured by every entry point.** A day zeroed in the
  availability calendar (or an unavailable period) holds no training after ANY
  entry point runs, including regenerate and auto-recalc; re-saving the same
  calendar repairs a plan that violates it. No such session reaches the
  calendar push. (H2; D5)
- **6.2 Every planned training session serves a workout file** that exists in
  the active library, on every entry point, the recovery ramp included; and
  the ramp's weeks run Monday–Sunday like every other week. (H3)
- **6.3 Past days are a record.** No entry point removes or rewrites a session
  dated before today, done / dismissed / missed / pending alike, the
  recovery-ramp path included. (H1 ramp path; D6)
- **6.4 A taper week is capped** even with no full build week before it
  (micro-plans): its load does not exceed the rider's normal week scaled by
  the taper fraction, and never exceeds the last build week. (H4, L5)
- **6.5 extend_continuous_plan applies the same weekly volume ceiling** as
  generate: no appended week exceeds its target by more than the tolerance
  generate uses. (M2)
- **6.6 Races are marked and B races tapered on every entry point**, extend
  included: a race added beyond the continuous plan's horizon is marked with
  its mini-taper once the plan reaches it. (M1)
- **6.7 A week's budget is net of what was ridden** (D2): a mid-week
  generate/regenerate after a heavy Mon–Wed does not push the calendar week
  above its target; no pass after the week's budget is set may add a hard
  session that takes the week over it. (M3)
- **6.8 A session's TSS and duration match what it serves**: after any swap
  (Rønnestad included) or re-match, `tss_estimate` is the served file's load
  at the rider's FTP (within the tolerance the library uses) and the
  description's minutes equal `duration_min`. (M4, L4)
- **6.9 Day caps and rest days**: the race-eve opener never lands on a
  declared rest day (M6); an FTP-test slot is the test file's length, never
  stretched or cut to the day's hours (L1); a 0-hour day that is not a rest
  day holds no session (L3).
- **6.10 The auditor sees what it missed** (D7: it reads the served file, the
  goal and the rides, not a copied type set): `plan_invariants.audit` gains
  rules for NaN/negative load, a race missing from its day, a taper heavier
  than the last build, a hard slot serving easy content, a training session
  with no file, ACWR above 1.3 (reported, not enforced), and its
  daily_duration_cap honours per-date overrides. Each rule fires on a planted
  violation (negative control) and not on legitimate 3 h override days.
- **6.11 Same inputs, same plan.** Two processes given identical inputs and a
  frozen clock produce identical plans (the audit saw this week's planned TSS
  vary 151–286). Find the source of the variation and remove it; do not mask
  it by seeding.

## Not in this round

H5, M5, M7 enforcement, today-session gates (prescription changes, above);
overhaul Step 6 itself.

## Decisions taken while landing it (2026-09-24)

Recorded so they are not relitigated without new evidence.

- **An unload week's load is training load, without the FTP test and the
  race** (c9e5acff). One definition for the planner's unload pass
  (`_enforce_stepback_is_lightest`), the auditor (`check_stepback_lightest`)
  and `stepback_looks_lighter` (PR #23), which already read it that way
  (`plan_invariants._training`). *Why:* the test is a measurement placed in
  the unload week on fresh legs (Allen & Coggan); since 6.9 it is its file's
  length, so a week holding nothing but the test read heavier than a light
  build week (weekend-event8: 80 against 78).
- **Ridden load has one owner**, `plan_invariants.rides_once` /
  `activities_once` (d07336dc). The budget, the auditor, the chronic load and
  the week view read rides once, cycling only; fitness and fatigue
  (local CTL/ATL, the readiness ACWR, daily adapt) read every sport once. Of
  two recordings of one effort the power meter's wins, then the one a session
  was matched to, then the shorter.
- **A reasoned rest carries `rest_reason`**, set where the rest is written
  (83efd441); nothing reads rest descriptions to decide anything.
- **A moved missed session is restored in the final step's record step**
  (04a8eb72), from the plan's `auto_moves` record, on every entry point.

## What the golden and the sweeps show that is not a finding

`under_delivery` rises: generate 80 -> ~107 (28 days x 13 riders), entries
455 -> 552 (the second evaluation's sweep). Cause, measured: 6.8. With the
final step's re-stamp disabled the generate sweep reads 83, base's figure;
the builders size a slot at the type's TSS per hour (z2 45/h, recovery 30/h)
and the files they serve are lighter (a 50-min "recovery" file carried 53.6
TSS; its served load is 22). The week's budget is unchanged, what the rider
is told the week holds is now what it holds, and the final step enforces --
it does not add sessions back. Sizing slots from the files they will serve is
a planning change (the builders'), not this round's.
