# Target-delivery contract — plans must carry the load they plan

Branch `slim/9-target-delivery`, base `slim/8-planner-finalize` d190fa82
(production since 2026-09-24). Research of 2026-09-25 (probes and tables:
`~/Documents/dq-audit-probes/delivery/`, `out/headlines_final.txt`,
`attribution_round1.txt`, `rest_*.txt`) measured production delivering
0.824 of its weekly targets on the generate sweep (364 plans, 3,808 weeks)
and 0.798 on the entry sweep (882 runs); the owner's regenerate on
2026-09-24 would deliver 0.788 with 15 h/week available.

Where the shortfall goes (generate; entries; owner regenerate):
builder under-build within the rider's hours 55.9 %; 78.2 %; 97.9 % — the
sampler sizes each session by its file's length (~75 min) and nothing grows
easy volume toward the budget (full 7-day load rows: 501 budget minutes,
413 built); targets above hours × the phase's mix at served rates 24.7 %;
7.1 %; 0 (the ramp converts hours at `_TSS_PER_AVAILABLE_HOUR = 65`, the mix
carries ~47.4); intended by D1–D8 / 6.x ≈ 25 %. TSS/h mis-sizing is only
1.8 % (the implementer's earlier explanation was wrong). `hours_per_week`
is not enforced as a weekly total (81 generate weeks > 10 % over).

Owner's decisions (2026-09-25):
- **Build the budget, as the ramp intends** (Sep 11: "give the build the most
  it safely can": Couzens CTL + k, Gabbett 1.3 × chronic guard). Load weeks
  carry the ramp's budget; only easy volume grows; chronic — and so later
  targets — may rise up to the 1.3 × guard. The owner accepted that effect.
- Also: enforce weekly hours; an honest hours-to-TSS rate for the ramp's
  availability cap; show budget vs planned when a week cannot hold its budget;
  a budget/readiness rest in a build week no longer shapes the next recovery
  week ("patternplus"), with the auditor changed in the same commit (D7).

D1–D8 stand. The fill is D2 made real; D5 holds because sessions scale to the
LOAD BUDGET, never to free time, and never past the date's minutes or the
week's hours.

## Assertions

- **9.1 A load week carries its budget.** In load weeks (not unload, not
  taper, not race week), the built plan's served load reaches the week's
  budget within the ceiling's tolerance whenever the rider's hours and the
  phase's mix can hold it. Only easy sessions (z2 / long_z2) grow; no hard
  session is added, lengthened or intensified by this; adapted, tier-downed,
  rider-owned, race and opener sessions are untouched. Measured: generate
  load-week delivery ≥ 0.90 (research simulation 0.911), owner regenerate
  ≥ 0.95 (simulation 0.967), and no rider is given more minutes on a date than
  the date allows.
- **9.2 The fill lives where the ramp sees it.** The growth happens in the
  builder (before `LoadRamp.follow` reads the week), not as a late patch, so
  the chronic load the ramp carries forward is the load actually planned.
  Report the effect on later targets (week-over-week budget, ACWR) over the
  sweeps and for the owner, next 6 weeks — the owner accepted a rise up to the
  1.3 × guard; the guard itself must still hold (no week above 1.3 × its
  chronic, the auditor's ACWR rule).
- **9.3 Weekly hours are a ceiling.** No week's planned minutes exceed
  `hours_per_week` (prorated for a partial row) after any entry point — the
  final step enforces it, trimming easy volume first. (Research: 81 generate
  weeks > 10 % over today.)
- **9.4 A lengthened slot serves a file that fits it** (L-4's rule still
  holds after the fill): no new file_duration_mismatch versus d190fa82.
- **9.5 The ramp's availability cap uses the served mix's rate**, derived
  from the library (one owner for the number; research median 47.4 TSS/h for
  the base mix), not the constant 65. A target the rider's hours cannot hold
  is lowered to what they can, not left unreachable.
- **9.6 The rider sees the truth.** When a week's planned load is below its
  budget by more than the tolerance, the week view / API carries both numbers
  and the reason (hours cap vs other), and the dashboard shows it; the
  planned sum is never labelled as the target.
- **9.7 A reasoned rest does not reshape the next recovery week**
  (patternplus): build-week rest counts use the rider's planned rest pattern,
  and a build week holding a budget or readiness rest is not the "lightest
  build week" reference. The 2026-09-25 live case (Fri rested by budget →
  10-06 rested `unload`, 10-05 204/244) keeps 10-06 and 244. The auditor's
  `check_stepback_lightest` changes in the same commit, with its control.
- **9.8 Nothing already decided regresses:** 6.1–6.11, MF-1/MF-2, L-1..L-4
  tests stay green; idempotency holds on every entry point; the planner
  characterization golden moves only for 9.x reasons, read case by case.

## Out of scope
Changing the 1.3 × guard or the Couzens k; TrainingWeek mode; unload/taper
fractions; hard-session counts.

## Owner's decisions, second round (2026-09-25 afternoon)
- **9.9 The 1.3 × guard runs on every sync.** Each daily sync / update /
  reforecast caps the budgets of the weeks from today on at 1.3 × the load the
  rider actually carries into them (the rides, counted once, cycling only), so
  after a lighter stretch the coming targets come down instead of asking for a
  jump. No stored week reads over 1.3 × against the rider's real load after a
  sync (evaluator's sweep today: reforecast 52→82, reforecast_dict 58→88,
  refit 33→44 hits — these must fall to ≤ base). Past days stay a record.
- **9.10 Unload weeks carry their own budget too.** The easy-volume fill
  applies to unload weeks toward their own (lower) budget — easy-only, within
  the date and week hours, never above the unload budget; the unload rules
  (lighter than the builds, rest pattern per 9.7) still hold after the fill.
  Owner case: 10-05 must no longer sit at 261 of a 387 budget.

## Owner's decision, third round (2026-09-25 evening)
- **9.11 A guard-lowered budget is visible, with the way back.** When the
  1.3 × guard lowers a week's budget on a sync (9.9), the stored plan records
  it on that week — from, to, when, and the carried load it used — written by
  the plan's one writer, never kept elsewhere. The dashboard shows a warning
  for every such week ahead: the budget was lowered from X to Y because the
  recent load was Z, and a replan resets it, with the replan action right
  there (the regenerate the Plan tab already offers). The API serves the same
  record (served = stored). A replan clears the records it supersedes. The
  warning disappears when no stored week carries one.
- **Where the guard's load comes from (checked 2026-09-25):** no app call
  site passes `recent_weekly_tss`; `athlete_weekly_load(None, None)` reads the
  ride archive itself (ride_storage.chronic_weekly_tss, 28-day EWMA, rides
  counted once, cycling only), else CTL × 7. That is the one reader; keep it so.
