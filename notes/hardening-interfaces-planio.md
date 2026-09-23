# Hardening, Wave 1 PLANNER-IO: what app.py must call (for Wave 2)

Branch `hard/planner-io` (base `slim/6-planner-rules` 1294c6f0). Line numbers
below are **src/app.py at 1294c6f0** (this branch does not touch app.py).
Wave 1 built the APIs in training_planner.py / workout_facts.py /
icu_calendar_push.py; this file lists, site by site, what app.py must change
to finish each contract item.

## 3.4 — `/tmp` is not an allowed base for a user-chosen workout directory

File side is done (commit "The library caches never write through a symlink
planted at their temp path": both caches go through
`workout_facts.atomic_write_text`, a mkstemp + os.replace).

app.py, `_setup_path_allowed_bases()` (app.py:1196-1227):

- delete the non-Windows branch that appends `/tmp` and `/private/tmp`
  (app.py:1223-1225, the `else:` arm);
- the Windows `%TEMP%/%TMP%` arm (app.py:1216-1222) is the same hole on
  Windows; it was added so a frozen onefile build unpacked in `%TEMP%` could
  pass its own bundled path, which the `_BUNDLED_WORKOUT_DIR` /
  `_BUNDLED_GPX_DIR` bases (app.py:1208-1213) already cover. Drop `TEMP` and
  `TMP`; keep `LOCALAPPDATA` / `APPDATA` if the installer puts the app there.
- the HTTP 400 text at app.py:1418 ("must be under $HOME, ~/.domestique, or
  /tmp") loses "or /tmp".
- test: `_setup_path_allowed(Path("/tmp/x"))` is False; `/api/setup/save` with
  `workout_dir=/tmp/<dir with a .zwo>` answers 400 and writes nothing.
  Existing tests that save a `/tmp` workout dir must move it under the
  sandbox HOME (grep `setup/save` + `workout_dir` in tests/).

## 4.1 / 4.2 — one plan store

### The API (training_planner.py)

```python
tp.update_plan(json_path, mutate) -> dict | None
```

- Holds `tp._plan_write_lock` (an RLock) across read -> `mutate(plan)` ->
  `atomic_write_plan`.
- `mutate(plan)` gets the plan as it is on disk now (`None` if the file does
  not exist) and returns the dict to write (the same object mutated in place,
  or a new one), or `None` to write nothing.
- An exception from `mutate` writes nothing and propagates unchanged (a
  `ValueError` for a 400, `tp.NoCandidateWorkoutError`, ...).
- Returns what it wrote, or `None`. Response data travels by closure.
- **Async handlers call it as**
  `await run_in_threadpool(tp.update_plan, json_path, mutate)`
  (`from starlette.concurrency import run_in_threadpool`, already used at
  app.py:10513). **Never call `update_plan`, `atomic_write_plan` or
  `plan_write_lock()` directly from an `async def`**: they wait on a
  threading lock, and waiting on the event loop stalls every request (audit:
  1.84 s; test: 2.7 s for `/api/version`). Sync `def` routes and background
  threads call it directly.
- Fetch plan-independent inputs (`cached("training", get_today_metrics)`,
  `fetch_wellness`, `db.query_activities`, `tp.load_workout_library()`,
  `_planning_ctl()`, `_planner_athlete()`, `_chronic_weekly_tss_safe()`)
  BEFORE the call where the code allows: whatever runs inside `mutate` holds
  every other plan writer. Anything that reads the plan stays inside.

Why a lock and no version check: every plan writer runs in this one process
(uvicorn workers=1; the CLI never writes current_plan.json). A lock held
across the whole read-modify-write makes the lost update impossible; a
version check would only be needed if the read had to happen outside the
lock, which nothing requires. The longest holder is a regenerate / recalc
(one plan build, seconds); other writers wait in the threadpool, not on the
event loop. `ponytail:` note in the docstring names the ceiling (a second
writing process would need a file lock).

Readers need no lock: `atomic_write_plan` replaces the file with
`os.replace`, so an unlocked reader sees the old or the new file, whole.

### The mechanical conversion (worked, tested: see Appendix A)

```python
    out = {}

    def _reply(r):              # early exit: remember the response, write nothing
        out["r"] = r
        return None

    def _mutate(plan):
        if plan is None:
            return _reply(JSONResponse({"error": "No active plan found"}, 404))
        # ... the handler's existing body from just after `plan = json.load(f)`
        #     to just before `tp.atomic_write_plan(json_path, plan)`, with every
        #     `return X` turned into `return _reply(X)` ...
        out["r"] = {...the success response...}
        return plan

    try:
        await run_in_threadpool(tp.update_plan, json_path, _mutate)   # async def
        # tp.update_plan(json_path, _mutate)                          # sync def
        return out["r"]
    except Exception:
        ...the handler's existing except...
```

Keep the `json_path.exists()` pre-check (it answers 404 without a thread
hop); the `plan is None` branch covers a delete in between.

### Every site (app.py @ 1294c6f0)

"R" = the unlocked read to move inside; "W" = the write to replace;
"L" = an existing `plan_write_lock()`; "loop!" = the lock is taken on the
event loop today (4.2).

| # | Site | Handler (kind) | Today | Conversion |
|---|------|----------------|-------|------------|
| 1 | L 341 | `_adopt_legacy_root_plan` (sync, boot, before serving) | L + `_rotate_plan_backups` + `shutil.copy2(root, prof)` | Not a read-modify-write (whole-file adoption of the legacy root plan). Leave under `tp.plan_write_lock()`. Out of scope, noted: copy2 onto the live file is not atomic. |
| 2 | L 3849, W 3866 | `api_readiness_revert_cap` (async) — **loop!** | L; R 3850-3851 | `_mutate` = the loop 3852-3869; return `plan` iff `restored`, else `None`. `restored` via closure. `await run_in_threadpool(...)`. |
| 3 | R 3924-3925, W 4049 | `api_readiness_apply_tier_down` (async) | unlocked | `_mutate` = 3926-4048. `_reply` for the 404 (3937) and the `ok: False` no-ops (3940, 3951, 3954, 3958). Hoist `tp.load_workout_library()` (3976), `db.query_activities(days=120)` (4016) and `cached("training", get_today_metrics)` (4020) above the call. |
| 4 | R 4159-4160, W 4342 | `api_plan_auto_adjust` (async) | unlocked | `dry_run` never writes: leave its unlocked read. Real path: `_mutate` = 4166-4342, returns `working` iff `actions` else `None`; `_reply` for the 500 at 4302. Hoist library (4245), activities/training (4309-4315). A write error keeps the 500 at 4345 (catch around the call). |
| 5 | R 9335-9336, W 9378 | `api_today_session_persist` (async) | unlocked | `_mutate` = 9337-9378; `_reply` for the 404 at 9376. |
| 6 | W 9884 | `_advance_continuous_deload` (sync helper), reached from `_maybe_advance_continuous_deload` (9759), whose only caller is `_maybe_auto_reforecast` 11447-11455 **after** its lock is released, on a fresh unlocked read | unlocked | Delete the write at 9884 (the helper already mutates `plan` in place). Replace 11447-11455 with `tp.update_plan(json_path, _deload)` where `_deload(plan)`: return `None` unless `_plan_is_continuous(plan)` (11449); `before = plan.get("deload_advance")`; `_maybe_advance_continuous_deload(plan, json_path)`; return `plan` iff `plan.get("deload_advance") is not before`, else `None`; set `written = "deload_advanced"` as today. **Tests:** 5 tests in tests/test_340_continuous_w2.py (TestDeloadAdvance::test_monotony_trip_converts_current_week, ::test_acwr_trip_converts_current_week, TestDeloadRevert::test_revert_restores_remaining_days_and_latches, and two more) call `_maybe_advance_continuous_deload` directly and read the FILE back; deleting the write alone fails them (review: 5 failed / 33 passed). Update them to call it through `tp.update_plan(path, ...)` as the new caller does. |
| 7 | R 9906-9907, W 9928 | `api_continuous_deload_revert` (async) | unlocked | `_mutate` = 9908-9928; `_reply` for `nothing_to_revert` (9910) and `week_passed` (9916). |
| 8 | L 10458, W 10463 | `_maybe_heal_plan_files` (sync; boot 722 and `_heal_plan_on_disk` via run_in_threadpool 10514) | L; R 10459-10460 | `stats = None`; `_heal(fresh)`: `stats = tp.heal_unmatched_sessions_dict(fresh, library, hr_bias=_hr_bias())`; return `fresh` iff `stats["healed"]`. Then, outside, the `_PLAN_HEAL_SEEN` stamp and `plan_data.clear(); plan_data.update(written)` when written. Library already loaded before (10457). |
| 9 | R 11158-11171, W 11335 | `api_plan_generate` (async) | unlocked (reads the old plan's availability blocks, writes a new plan) | `_mutate` = 11155-11335 (from the `_existing_plan` read to the write; the old plan arrives as the argument instead of the `open`); return `plan_dict`. No `return` inside that range (11274 is a nested def). `ValueError` from `tp.generate_plan` propagates to the existing 400 at 11356-11360. **Corrupt plan:** today generate catches `json.JSONDecodeError` on its read and builds a fresh plan -- it is the action that repairs a corrupt file. `update_plan` raises that JSONDecodeError (a ValueError) before `mutate` runs, which would turn into the 400 with the parse error. Keep the repair: `try: tp.update_plan(json_path, _mutate)` / `except json.JSONDecodeError: tp.atomic_write_plan(json_path, _mutate(None))` (the corrupt file rotates into `.bak`, nothing lost), with `_mutate` treating `None` as "no old availability". Hoist `cached("training", get_today_metrics)` (11188) and the prefs read (11024). |
| 10 | L 11393, W 11443 | `_maybe_auto_reforecast` (sync: sync thread, 17069, 18332) | L; R 11397-11398 | `_mutate` = 11399-11443; the two `return None` (11420, 11440) become `return None` from `_mutate` (nothing written; `written` stays None). Hoist `db.query_activities(days=120)` and `cached("training", ...)` (11424-11425) above. Then site 6. |
| 11 | R 11469-11470, W 11574 | `api_plan_reforecast` (async) | unlocked | `_mutate` = 11471-11574; hoist training (11503) and activities. |
| 12 | R 11610-11611, W 11633 | `api_mark_unavailable` (async) | unlocked | Validate the body (11612-11627) before the call (it does not read the plan); `_mutate` = the append + write. |
| 13 | R 11659-11660, W 11715 and W 11760 | `api_save_availability` (async) | unlocked, two writes | One `_mutate` covering 11661-11760; both paths return `plan`; the early `sessions_modified: 0` response (11716) via `out`. Hoist training (11727) and activities. |
| 14 | R 12361-12362, W 12379 | `api_plan_regenerate_dynamic` (async) | unlocked | Hoist training (12370); `_mutate(plan)`: `plan_dict, regen_info = _regenerate_plan_dict(plan, ...)`; return `plan_dict`. `ValueError` -> existing 400 (12384-12387). |
| 15 | R 12431-12432, W 12445 | `api_plan_add_race` (async) | unlocked | Body validation (12407-12418) stays before; hoist training (12439); `_mutate` = add event + `_regenerate_plan_dict` + return `plan_dict`. Note: it uses `await request.json()` (12407), not `_get_json_body` (7.5 is Wave 2's). |
| 16 | L 12478, W 12497 | `api_plan_update` (async) — **loop!** | L; R 12479-12480 | `_mutate` = 12481-12497; `skipped` (12493-12496) returns `None` + `_reply`. Hoist training (12481) and activities. |
| 17 | R 13743-13744, W 13837 | `api_plan_move_session` (async) | unlocked | Worked example, Appendix A. The lazy re-read (13796-13806) is redundant under the store (the argument IS the disk copy); harmless to keep, simpler to drop. |
| 18 | L 15630, W 15649 | `api_plan_rematch` apply=1 (async) — **loop!** | L; R 15631-15632 | `_mutate` = 15633-15649; `no_current_week` (15644) via `_reply`. apply=0 (15654-15665) is read-only: unchanged. |
| 19 | R 15728-15729, W 15748 | `api_plan_re_draw` (async) | unlocked | `_mutate` = 15730-15748; `_reply` for 15736, 15740, 15744, 15745. The digit-resolution read at 15710-15711 is read-only: unchanged. |
| 20 | R 16057-16058, W 16060 | `api_plan_accept_redraw` (async) | unlocked | `_mutate`: `out["r"] = _accept_redraw_apply(plan, day_iso, body)`; return `plan`. ValueError -> existing 400 (16062-16063). |
| 21 | R 16260-16261, W 16267 | `api_plan_swap_type` (async) | unlocked | `_mutate` = 16262-16267. |
| 22 | R 16324-16325, W 16352 | `api_plan_ftp_test_type` (async) | unlocked | Hoist `tp.load_workout_library()` (16336); `_mutate` = 16326-16352; `_reply` for 16329, 16331, 16339. |
| 23 | R 16395-16396, W 16420 | `api_plan_dismiss_session` (async) | unlocked | `_mutate` = 16397-16420; `_reply` for 16407, 16410. |
| 24 | R 16441-16442, W 16571 | `api_plan_auto_recalc` (sync def) | unlocked | Worked example, Appendix A. Hoist `cached("training", ...)`, `_planning_ctl()`, `fetch_wellness` + eFTP pick, `_planner_athlete()`, `_chronic_weekly_tss_safe()` (16465-16531) above the call; the fresh / no_change branches return `None` via `_reply`. |

Other plan writers, for completeness (no change needed):
- `_maybe_restore_plan_from_backup` (app.py:102-140): boot-only, runs before
  the server serves, restores a missing live file from `.bak*` via
  tmp + replace. Not a read-modify-write.
- `tp.rewrite_stale_plan_classifications` (boot, app.py:703): moved onto
  `update_plan` in this branch.
- No other module writes current_plan.json (grep `atomic_write_plan`,
  `current_plan`).

After the conversion:
- `tests/test_planner_fixes.py::TestFix4AtomicWrites::test_all_sites_use_atomic_write_plan_helper`
  counts `tp.atomic_write_plan(json_path, plan|plan_dict)` calls in app.py
  (expects 22). It must change to: zero `tp.atomic_write_plan(` and zero
  `plan_write_lock()` inside `async def` bodies in app.py, and every writer a
  `tp.update_plan(` call (sites 2-24 minus 1 -> 21 or so, after 6 folds into
  10). The reference conversion of sites 17 and 24 alone drops the count to
  20 and fails it, as expected.
- The 4.2 test drives move-session only. The three handlers that take
  the lock on the event loop today (sites 2, 16, 18, "loop!") are covered
  only by the structural rule above (no lock call inside an `async def`) --
  write it as a test, not just a count.
- Remove the two `@WAVE2` strict-xfail markers in
  `tests/test_hardening_plan_store.py`; the tests are the contract:
  `test_move_racing_auto_recalc_both_persist` (4.1; asserts BOTH writes
  persist -- the move, and the recalc's `recalc_date` -- so converting only
  one of the two handlers fails it: verified, "the recalc was overwritten by
  the move's stale copy") and
  `test_event_loop_answers_while_a_slow_writer_holds_the_plan` (4.2). With
  only sites 17 and 24 converted (Appendix A) both pass (3/3 runs); unconverted,
  they fail with "the move was overwritten by the recalc's stale copy" and
  "/api/version took 2.70s behind the plan lock".

## 5.1 — one owner of the active workout directory

### The API (training_planner.py)

```python
tp.active_workout_dir() -> Path     # the planner's WORKOUT_DIR, read at call time
```

The planner's `WORKOUT_DIR` is the owner: it is what `load_workout_library`,
`match_zwo` and the facts layer plan from, so the files a plan names live
there. `pm.apply_training_dirs()` (profile_manager.py:663, the storage
agent's file this wave) is the resolver that sets it; it already runs on
every `pm.switch()` and once at boot (app.py:642).

In this branch the calendar push (`icu_calendar_push._desired_events`, its
one directory read) goes through `app.active_workout_dir()` instead of the
bare `app.WORKOUT_DIR` global, so it follows the accessor once Wave 2 points
the accessor at the planner.

### What app.py must change

1. `active_workout_dir()` (app.py:451-460): `return tp.active_workout_dir()`.
   Every app read already goes through it (5281-5591, 5726, 5762-5764,
   5963-5965, 6961-6978, 7890, 12549, 15502), and so does
   `launcher.py:702` (`app.WORKOUT_DIR` -> `app.active_workout_dir()` there
   too: it is the last bare read outside app.py).
2. `_apply_profile_paths()` (app.py:471-522): stop resolving the workout
   directory; call `ProfileManager.get().apply_training_dirs()` (no-op
   without an active profile) and keep only the GPX resolution. setup_save
   already calls `_apply_profile_paths()` after writing the profile's
   user_paths.json (app.py:1532), and the switch path already registers it
   (app.py:662) -- so both now move the planner, which is the only copy.
3. Delete the app's own `WORKOUT_DIR` global and its import-time
   user_paths.json resolution (app.py:423 and the workout half of 429-448),
   plus `global WORKOUT_DIR` in `_apply_profile_paths` and `setup_save`
   (1265). Keep `_BUNDLED_WORKOUT_DIR` (1208, the setup allow-list).
   `diag` already reports `tp.WORKOUT_DIR` (19975-19977).
4. Tests that bind `app_module.WORKOUT_DIR = ...` must bind
   `tp.WORKOUT_DIR` instead. Measured by the reviewer with steps 1-2 applied:
   25 failures, in test_363_block_eval_production_path,
   test_calendar_push_workout, test_icu_push and
   test_profile_path_rebinding; test_account_flow, test_capacity_cap and
   test_plan_entry_continuity bind it too but still pass (update them
   anyway once the global is gone). test_profile_path_rebinding (whose `test_apply_profile_paths_still_rebinds`
   pins `WORKOUT_DIR = wp` in the source; it becomes "the accessor returns the
   planner's directory").
5. Remove the strict-xfail marker on
   `tests/test_hardening_workout_dir.py::test_setup_save_moves_the_planner_and_the_app_together`.
   Verified: with steps 1 and 2 alone (a 5-line scratch change: the accessor
   returns `tp.active_workout_dir()`, `_apply_profile_paths` ends with
   `ProfileManager.get().apply_training_dirs()`), that test passes -- the app
   and planner directories are the same path, a file the planner would plan
   downloads 200, the push reports no `file_missing`. Unchanged it fails:
   `app.active_workout_dir()` = the new folder, `tp.active_workout_dir()` =
   the bundled library.
6. Known gap, not in the contract's wording: sessions planned BEFORE the
   directory change still name files from the old library and 404 until the
   plan is regenerated (the self-heal only fills sessions with no file). The
   e2e test checks files planned after the save.

profile_manager.py (storage agent / Wave 2, optional): `apply_training_dirs`
and `_apply_profile_paths` hold the same three-step resolution twice; once
step 2 lands, only `apply_training_dirs` resolves. training_planner.py's own
import-time `user_paths.json` block (root-level file, pre-profile) sets the
initial value before any profile is active; boot then overrides it.

## 5.5 — `sportInfo[0]` reads in training_planner.py

One read (training_planner.py @ this branch):

- training_planner.py:15896-15897, `check_and_auto_apply_eftp`:
  `si = rec.get("sportInfo") or []` / `eftp = si[0].get("eftp") if si else None`
  -> the storage agent's cycling-sportInfo helper from training.py (e.g.
  `eftp = (training.cycling_sport_info(rec) or {}).get("eftp")`, whatever it
  is named). The docstring at 15865 says "sportInfo[0].eftp"; change it to
  "the cycling sportInfo entry's eftp". training_planner already imports from
  training (line 110), so no new import edge.

Not training_planner, for whoever sweeps 5.5: app.py has 13 `sportInfo`
mentions (one is the eFTP pick in `api_plan_auto_recalc`, 16521-16527);
ride_storage.py:1831, db.py:397 / 466, fitness_estimation.py:137 and
profile_manager.py:240 also index or document `sportInfo[0]`.

### Appendix A — reference conversion of sites 17 and 24 (verified)

**It is the minimal mechanical conversion, NOT the recommended one:** it
leaves `cached("training", ...)`, `_planning_ctl()`, `fetch_wellness` and
the other plan-independent fetches INSIDE `_recalc`, i.e. an intervals.icu
fetch can run while the plan lock is held. Do the hoists the table lists
(row 24); the tests do not notice because they stub `fetch_wellness`. Note
also that `_planning_ctl` / `_fitness_state` / `_chronic_weekly_tss_safe`
call `cached(...)` again inside `_apply_plan_update` and
`_regenerate_plan_dict` (sites 10, 14, 15, 16): hoisting the outer call only
warms the cache; if a failed fetch is not cached (contract 1.3), the retry
happens inside the lock.

Generated by a scratch script and run against this branch: the two strict
xfail tests pass, and the move-session / auto-recalc suites
(test_move_session_lazy_load, test_plan_api, test_recalc_preserves_state,
test_regen_goal_fidelity, test_reads_do_not_write, test_event_fixes_w2,
test_332_tab_flatten, test_facts_infra_failsafe,
test_plan_is_a_function_of_its_goal, test_planner_fixes) stay green except
the site-count test above. Not committed (app.py is Wave 2's).

```diff
diff --git a/src/app.py b/src/app.py
index 844bc06b..555cc405 100644
--- a/src/app.py
+++ b/src/app.py
@@ -13739,9 +13739,15 @@ async def api_plan_move_session(request: Request):
     if src_iso == dst_iso:
         return JSONResponse({"error": "date and new_date must differ"}, 400)
 
-    try:
-        with open(json_path, encoding="utf-8") as f:
-            plan = json.load(f)
+    out = {}
+
+    def _reply(r):
+        out["r"] = r
+        return None
+
+    def _move(plan):
+        if plan is None:
+            return _reply(JSONResponse({"error": "No active plan found"}, 404))
 
         # v4.1.1 FIX-PICKER-MOVE (Bug E): week-boundary check now uses ISO
         # Mon-Sun — NOT the iterate-stored-weeks approach. The old check
@@ -13760,10 +13766,10 @@ async def api_plan_move_session(request: Request):
         src_iso_week = src_d.isocalendar()[:2]  # (iso_year, iso_week)
         dst_iso_week = dst_d.isocalendar()[:2]
         if src_iso_week != dst_iso_week:
-            return JSONResponse(
+            return _reply(JSONResponse(
                 {"error": "move must stay within the current ISO week (Mon-Sun)"},
                 400,
-            )
+            ))
 
         weeks_data = plan.get("weeks", [])
 
@@ -13807,13 +13813,13 @@ async def api_plan_move_session(request: Request):
                 _log.debug(f"move-session lazy-reload failed: {_e}")
 
         if src_week_idx is None:
-            return JSONResponse(
+            return _reply(JSONResponse(
                 {"error": "source_session_not_found", "date": src_iso}, 422
-            )
+            ))
         if dst_week_idx is None:
-            return JSONResponse(
+            return _reply(JSONResponse(
                 {"error": "target_week_not_found", "date": dst_iso}, 422
-            )
+            ))
 
         # FC3 (v2.5.0, L3-2/L3-10): the race entry is goal-owned — a drag must
         # not move it, and nothing may be dropped ONTO it. The sanctioned write
@@ -13821,22 +13827,26 @@ async def api_plan_move_session(request: Request):
         for w in plan.get("weeks", []) or []:
             for s in w.get("sessions", []) or []:
                 if s.get("day") in (src_iso, dst_iso) and s.get("is_race"):
-                    return JSONResponse(
+                    return _reply(JSONResponse(
                         {"error": "race day is fixed — edit the race instead"},
                         400,
-                    )
+                    ))
 
         # Mutation is shared with the auto-reschedule path (_apply_move_session)
         # so both apply the identical same-week relocation + rest-stub rewrite.
         moved_payload = _apply_move_session(plan, src_iso, dst_iso)
         if moved_payload is None:
-            return JSONResponse(
+            return _reply(JSONResponse(
                 {"error": "source_session_not_found", "date": src_iso}, 422
-            )
+            ))
 
-        tp.atomic_write_plan(json_path, plan)
+        out["r"] = {"ok": True, "moved": moved_payload, "vacated": src_iso}
+        return plan
 
-        return {"ok": True, "moved": moved_payload, "vacated": src_iso}
+    try:
+        from starlette.concurrency import run_in_threadpool
+        await run_in_threadpool(tp.update_plan, json_path, _move)
+        return out["r"]
 
     except Exception:
         _log.exception("Plan move-session failed")
@@ -16437,9 +16447,15 @@ def api_plan_auto_recalc():
     if not json_path.exists():
         return {"action": "no_plan"}
 
-    try:
-        with open(json_path, encoding="utf-8") as f:
-            plan = json.load(f)
+    out = {}
+
+    def _reply(r):
+        out["r"] = r
+        return None
+
+    def _recalc(plan):
+        if plan is None:
+            return _reply({"action": "no_plan"})
 
         # Check if recalc needed (>7 days since last)
         last_recalc = plan.get("recalc_date") or plan.get("generated", "")
@@ -16473,7 +16489,7 @@ def api_plan_auto_recalc():
                     except Exception:  # noqa: BLE001 — decorative readiness only
                         _log.exception("auto-recalc: fresh-branch readiness failed (returning fresh)")
                         readiness = {}
-                    return {"action": "fresh", "days_since_recalc": days_since, "event_readiness": readiness}
+                    return _reply({"action": "fresh", "days_since_recalc": days_since, "event_readiness": readiness})
             except (ValueError, TypeError):
                 pass
 
@@ -16539,7 +16555,7 @@ def api_plan_auto_recalc():
         )
 
         if recalc_info.get("action") == "no_change":
-            return {"action": "no_change", **recalc_info}
+            return _reply({"action": "no_change", **recalc_info})
 
         # Save updated plan — START from a shallow copy of the ORIGINAL plan so
         # every top-level key survives (availability calendar, adoption/regen
@@ -16568,9 +16584,12 @@ def api_plan_auto_recalc():
         else:
             plan_dict.pop("phase_weeks_status", None)
 
-        tp.atomic_write_plan(json_path, plan_dict)
+        out["r"] = {"action": "recalculated", "plan_json": plan_dict, **recalc_info}
+        return plan_dict
 
-        return {"action": "recalculated", "plan_json": plan_dict, **recalc_info}
+    try:
+        tp.update_plan(json_path, _recalc)
+        return out["r"]
 
     except Exception:
         # SEC3: don't leak exception text.
```
