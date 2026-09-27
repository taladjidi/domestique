# Single-source contract — ONE plan, ONE function touching it

Owner's requirement (2026-09-25, verbatim): "Make sure that the single source
of truth approach is bullet proof. There should be ONE plan, and ONE function
touching it."

Branch: `slim/10-one-plan`, on top of `slim/9-target-delivery` once that round
lands (one writer per file: training_planner.py belongs to the delivery round
until then). Audit of d190fa82 (checked against slim/9 f049fe11), its probes
and outputs: `~/Documents/dq-audit-probes/single-source/` (static_scan.py,
rt_audit.py, get_reach.py, s9_preview.py, plants12/, plants_extra/, out/).

What the audit proved: every HTTP-route and cron write already goes through
`training_planner.update_plan`, and no GET writes the plan. It is not
bulletproof: boot/migration/corrupt-generate write around it, derived copies
are persisted and served, four GETs serve an answer other than the stored plan,
and the plan's identity is a process global.

## Definitions
- **The plan** = one file per profile, `profiles/<id>/plans/current_plan.json`,
  plus its rotated backups. Nothing else is a plan.
- **The owner** = a new leaf module `plan_store.py`, the only module that may
  name the plan path or touch its files.

## Assertions

- **10.1 One writer.** `plan_store.update(profile_id, mutate, *, reason)` is the
  only code path that writes, renames, copies over, rotates or deletes a plan
  file. Boot restore-from-backup, legacy root adoption, profile migration, and
  generate-on-a-corrupt-plan (`mutate(None, corrupt=True)`) are store
  operations through the same private writer. (Audit findings 1, 2, 3, 5.)
- **10.2 One reader.** `plan_store.read(profile_id)` is the only code that
  opens a plan file (46 hand-built paths today: 44 in app.py,
  icu_calendar_push.py:153, power_curve.py:762). A corrupt plan raises one
  `PlanCorrupt`, and every route gives the same answer for it (today four
  routes give four answers).
- **10.3 The plan's identity is the profile, not a global.** Every store call
  takes the profile id captured when the request or job starts; `tp.PLAN_DIR`
  is not how a writer finds its plan. A switch landing mid-operation cannot
  make one profile's writer use another profile's inputs or file (finding 10).
  Legacy root adoption names its owner; with more than one profile it refuses
  and logs rather than adopting into whichever profile is active (finding 1).
- **10.4 No derived plan is persisted.** The plan markdown is rendered from
  the JSON when requested, never written as `plan_*.md`; the premigration
  snapshot becomes a store backup (finding 4, 7). The planner CLI's
  `--push-icu` path and any CLI plan write are removed or made to go through
  the store (audit integ #8, finding "calendar mirror").
- **10.5 What the rider is served is the stored plan.** For every GET that
  returns sessions, targets or week totals, each served (day, type, duration,
  TSS, file) equals the stored plan's. Specifically:
  - `/api/today-session` serves the stored session; a readiness adjustment
    becomes a plan write on the POST that brings the input (daily-log, sync),
    through the store, so the ICU event and the Bryton get it too (finding 6).
  - `/api/plan`'s "Raw plan" is rendered from the stored JSON (finding 7).
  - `/api/plan/preview` shows the stored phases while its form equals the
    stored goal, and is labelled a draft otherwise (finding 8; slim/9 widens
    the gap).
  - `/api/plan/daily-adapt` is a dry run of the real on-sync adaptation
    (`_apply_plan_update`), not a second algorithm (finding 9).
- **10.6 Mechanical guards — each must fail on a planted violation:**
  - **G1 write token**: the store's private writer raises unless called by
    `update` (or a named store operation) for that same path, via a ContextVar.
  - **G2 file audit hook**: `sys.addaudithook` installed at app start and in
    the test session refuses any write/rename/replace/remove/copy/move/rmtree
    under a `plans/` dir without the token, and any store write on the event
    loop thread; subprocess with a plans path is refused.
  - **G3 route sweep test**: every non-GET route has a body and reaches its
    write (or is listed as no-op with a reason); boots, background hooks and
    the cron sequence run; zero unowned plan-dir writes, zero GET writes; the
    audit's plants are its self-test.
  - **G4 static test**: the scanner in CI — plan-file access outside
    plan_store is an allowlist that may only shrink (target: empty); the 12
    evaluator plants + the extra plants are caught; the negative control is
    clean.
  - **G5 served-equals-stored test** (the class G1–G4 cannot see): every
    plan-returning GET's served sessions equal the stored plan's; planner
    builders reachable from GET handlers are an allowlist of labelled drafts.
  - **G6 inter-process**: `fcntl.flock` on the plan file in the store's writer,
    so a second app instance or CLI cannot interleave.
- **10.7 Nothing decided regresses**: all 6.x, 9.x, MF/L tests green;
  idempotency on every entry point; the owner-plan comparison (sandbox copy)
  shows no change to the stored plan from this round except where 10.5 moves a
  readiness adjustment from the GET into the stored plan.

## Known limits (recorded, not solved here)
External mirrors (intervals.icu events, Bryton files) lag the plan by the
push debounce and the reconcile policy; this round bounds the lag after every
store write (the store fires the post-write callback for EVERY write,
including boot and migration) but cannot make them transactional.

## Widened 2026-09-25 (from the delivery round's findings): one ride store, one reader
slim/9 (433c6067) made `ride_storage.ride_record()` the one reader for every
LOAD fact (carried_in, carried_step, the week under way) — instrumented. What
still reads rides elsewhere, to move onto the same reader in this round:
- **10.8** `detect_plan_gaps` (regenerate's absence ramp) reads
  `db.query_activities`, a different store; move it to the ride record.
- **10.9** the auditor's `budget()` / weekly_volume / under_delivery take a
  caller's `rides` list; they read the record (tests through the one seam,
  conftest's `ride_record` fixture), as check_acwr now does.
- **10.10** the remaining `db.query_activities` readers (session reconcile,
  monotony/strain, the auto-reforecast trigger, reforecast/update handlers)
  and `activity_rows_between`'s direct `load_all_rides`: one reader for the
  rider's rides, or a written reason why the SQLite mirror is a different
  fact (it is an intervals.icu mirror; the ride record is Domestique's own) —
  and then the two must be shown to agree, with a test.
Held week vs same-day regenerate differing is policy, not a reading: the
owner chose "a sync holds, never above built; replan resets" (9.9 amended,
9.11) — keep it.

## Carried over from the delivery round's final evaluation (eval12, 2026-09-26)
- **10.11** (= eval12 D-2) regenerate's `detect_plan_gaps(query_activities(120))`
  and the sync's reforecast G4 `_last_completed_week_acwr(query_activities)`
  are LOAD facts on the SQLite mirror: route through `ride_record()` with the
  field mapping (`started_at` vs `date`/`start_date_local` — a naive swap
  silently reads nothing). The generate endpoint's direct `load_all_rides`
  (days_since_last_ride) goes through the seam too.
- **10.12** (= D-3) one plan state for the projection: the ramp follows rows
  before finalize's passes, the guard after (6–8 % drift, fresh budgets
  silently trimmed); and `_entry_week_targets` still calls LoadRamp.advance
  without carried_step (preview phase labels) — one projection.
- **10.13** (= D-1) the guard and the auditor judge the same quantity (budget
  vs planned sessions): the fill never plans above the held budget.
- **10.14** (= D-5) `check_acwr`/`audit` read the ride record when not handed
  rides (today they assume nothing was ridden → 1.81× false reading).
