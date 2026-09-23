# Hardening contract — audit of 2026-09-23

The plan of record for `slim/7-hardening` (base `slim/6-planner-rules`,
1294c6f0). Findings and their reproductions: `notes/review/audit-2026-09-23.md`
in the refactor worktree (`~/Documents/domestique`). Owner's scope decision
(2026-09-23): root causes 1–5 and 7. Cause 6 (the planner's shared final step)
and every change to what a rider is prescribed are OUT of scope.

Each line below is a testable assertion. It is DONE only when:
- a test asserts it and passes;
- the same test FAILS with the fix reverted (state the mutation and the
  failure count in the commit message);
- the full hermetic gate shows no new failure against the same-day slim/6
  baseline (`~/Documents/dq-hard-gate/slim6-base.*`).

Design rule for every fix: one owner, the simplest thing that holds
(CLAUDE.md "ponytail" ladder). No new framework. Fix the shared function once
and move every caller onto it; do not patch one call site.

## 1. An empty or failed answer from intervals.icu is not a fact

- **1.1** A sync whose activity fetch returns `[]`, `null`, a non-list, or
  raises, prunes NOTHING. A prune happens only on a successful, non-empty
  answer that overlaps the local set in its window. The per-pass cap is not a
  flat 20 that a small archive fits under: a pass that would remove more than
  half of the window's local rides removes none and logs why.
- **1.2** Enriching a ride (DFA streams / FIT) marks it `icu_deleted` ONLY on a
  confirmed not-found (404/410). A timeout, network error, 429 or 5xx gives a
  retryable status that the next sync pass retries; after recovery the ride is
  computed.
- **1.3** When intervals.icu fails, the `training` metrics cache keeps serving
  the last good value (the cache's own stale-on-failure policy applies); a
  failure is never cached as `{}` for the full TTL.
- **1.4** A truncated body (`http.client.IncompleteRead` / `HTTPException`) is a
  retried network error, and never counts as an authentication failure.

## 2. What the rider typed survives anything intervals.icu does

- **2.1** `/api/icu/disconnect` and "switch account" remove the intervals.icu
  mirror but keep every rider-entered value: RPE (`rpe`, `rpe_at`,
  `rpe_scale`), `ftp_test_review`, manual/HRV4Training HRV, `is_race`. After a
  reconnect and a sync, all of them are back on their rides/days.
- **2.2** A ride record has one writer path: a per-ride locked, atomic
  (tmp + `os.replace`) read-modify-write. An RPE posted while a DFA
  write-back or a sync persist of the same ride is in flight survives both.
- **2.3** A manual (or settings-sourced) VO2max is not overwritten by the
  intervals.icu value; the icu metric upserts share one helper.

## 3. Files are written safely

- **3.1** `athlete.json` (and every profile JSON written through the same
  helper): concurrent writers produce no exception and a reader never sees
  an empty or torn file. Unique temp file, same-directory `os.replace`.
- **3.2** A corrupt (unparseable, non-empty) `athlete.json` is never replaced
  by defaults: it is moved aside to `athlete.json.corrupt-<ts>`, the event is
  logged at WARNING, and a subsequent partial write does not persist a
  near-empty file over the rider's data.
- **3.3** A failed plan write does not rotate the plan backups.
- **3.4** Library cache writes (`.library_index.json`, `.workout_facts.json`)
  never follow a symlink planted at their temp path; `/tmp` is not an allowed
  base for a user-chosen workout directory.

## 4. One plan store

- **4.1** Every read-modify-write of the plan goes through one store call that
  holds the plan lock across read, mutate and write. A move-session and an
  auto-recalc (or auto-adapt) racing each other both persist.
- **4.2** No async request handler blocks the event loop waiting on the plan
  lock: while the lock is held by a slow writer, `/api/version` answers in
  under 0.5 s.

## 5. One owner per fact

- **5.1** After `/api/setup/save` changes the workout directory, the app's and
  the planner's workout directories are the same path, planned-session
  downloads answer 200, and the calendar push does not skip for
  `file_missing`.
- **5.2** A rider with only imported FIT rides (no intervals.icu) gets a CTL
  and ATL computed from the same set of rides, and the week-activity collector
  (daily-adapt / rematch / missed detection) sees this week's rides.
- **5.3** Cached values are scoped to the profile: a fill for rider A still in
  flight when the switch to B happens is never served to B.
- **5.4** Switching profile while a sync pass is blocked in a slow fetch leaves
  syncing working for the new profile (the next pass runs and writes); the
  sync status reports whether the loop is alive.
- **5.5** eFTP, W′ and Pmax are read from the cycling entry of intervals.icu's
  `sportInfo` (Ride / VirtualRide), by one helper every reader uses; a Run
  entry listed first changes nothing.
- **5.6** Per-rider state lives in the active profile's directory:
  `readiness_cap_reverted`, `last_pr_toast`, `hrv_prompt_state`. One rider's
  revert does not suppress another's cap. Generate never reads the root
  `user_prefs.json`.
- **5.7** The calendar push's daily-reconcile gate and last-result status are
  per profile.

## 7. HTTP surface

- **7.1** Deleting a profile id that is not in the registry answers 404 and
  changes nothing; no sequence of deletes leaves the app with no active
  profile while a profile exists. Updating an unknown profile answers 404.
- **7.2** Requests whose `Host` is not an allowed host get 400. Allowed:
  `127.0.0.1`, `localhost`, `[::1]` (any port), plus hosts listed in the env
  var `DOMESTIQUE_ALLOWED_HOSTS` (comma-separated). The deployment sets it for
  the nginx vhost before this ships: production nginx forwards
  `Host: domestique.aladjidi.xyz` (`/etc/nginx/conf.d/zz-domestique.conf`), so
  the `domestique` user unit needs
  `DOMESTIQUE_ALLOWED_HOSTS=domestique.aladjidi.xyz`, or every proxied request
  gets 400. The SSH-tunnel path (Host `localhost:<port>`) is allowed already.
- **7.3** `/api/setup/pick-folder` never opens a GUI dialog from a request
  handler when no webview window exists: it answers immediately with an empty
  path and a reason.
- **7.4** No GET mutates state: the PR-toast drain is a POST, and the lazy
  intervals.icu sync is started by one explicit POST the dashboard calls, not
  as a side effect of GET routes.
- **7.5** A POST whose JSON body is not an object answers 400, not 500, on
  every route that reads a body.
- **7.6** `/api/plan/generate` rejects out-of-range or non-finite inputs
  (hours, plan weeks) with 400, using the setup limits.
- **7.7** The setup wizard's wellness prefill window is relative to today.
- **7.8** `/api/logs` has the same local-only guard as `/api/diag/*`.

## Not in this round (recorded so nobody re-derives them)

Cause 6 (holiday refill on regenerate/auto-recalc, fileless recovery ramp,
micro-plan taper, extend ceiling); prescription changes (3-day all-hard weeks,
capacity_cap sub-FTP, ACWR enforcement, today-session gates using the one
hardness predicate); fact disagreements facts #3, #6–#12; planner
non-determinism across processes (to investigate).
