# Hardening Wave 1 — storage: what Wave 2 must call

Branch `hard/storage`. Files owned in Wave 1: src/db.py, src/ride_storage.py,
src/profile_manager.py, src/cache.py, src/fitness.py, src/training.py.
Line numbers are for `src/app.py` / `src/power_curve.py` at 1294c6f0 (this
branch does not touch them).

Two strict-xfail tests turn into XPASS -> FAIL when Wave 2 lands the app.py
side; delete their `@WAVE2` / `xfail` marker in the same commit:
- tests/test_hardening_fetch_outcome.py::test_network_down_does_not_mark_the_ride_deleted
- tests/test_hardening_fetch_outcome.py::test_http_503_does_not_mark_the_ride_deleted
- tests/test_hardening_local_fitness.py::test_the_collector_itself_sees_a_fit_only_riders_week

## 1.1 prune — nothing to do in app.py

`ride_storage.prune_deleted_icu_records(ids, window_days)` now refuses an empty
or non-list answer and any pass removing more than half of the window's local
rides (still max 20). `training.fetch_activities` raises `ICUServerError` on a
2xx body that is not a list, which `_sync_icu_activities_locked` (app.py
~17655) already reports as `status: "fetch_failed"`. The call at app.py ~17735
can stay as it is.

## 1.2 DFA enrichment: `icu_deleted` only on a confirmed 404/410

New in training.py:

```python
training.Fetched(status, data)          # NamedTuple
# status: "ok" | "not_found" (404/410) | "no_data" (other 4xx, e.g. 422)
#         | "failed" (network, timeout, truncated body, 429, 5xx, auth, no creds)
training.fetch_activity_streams_result(activity_id) -> Fetched   # data: dict by stream type
training.fetch_activity_fit_file_result(activity_id) -> Fetched  # data: bytes
```

The legacy `fetch_activity_streams` / `fetch_activity_fit_file` still return
data-or-None (power_curve and the detail endpoint use them).

app.py `_augment_icu_record_with_dfa` (~17358-17580), inside `_fetch_and_compute`:
- step 2: `r = _training.fetch_activity_streams_result(external_id)`; use
  `r.data` when `r.status == "ok"`; remember `r.status`.
- step 3: `r = _training.fetch_activity_fit_file_result(external_id)`; same.
- return the dfa dict, or a marker saying why there is none. Mark
  `dfa_alpha1_status = "icu_deleted"` ONLY when both answers were
  `not_found`. Any `failed` -> `dfa_alpha1_status = "fetch_failed"` (not in
  the sticky set at ~17404, so the next sync retries it). `no_data` on both
  (a Strava-origin ride with no local streams) -> `"no_rr_data"` is the
  honest final state (nothing to compute from).
- the write-back (~17577): see 2.2 — merge through `ride_storage.update_ride`.

## 1.3 metrics cache — nothing to do in app.py

`training.get_today_metrics()` returns `cache.Unavailable()` (an empty dict,
falsy, `== {}`) on an ICU failure; `cache.cached` treats it as a failed fill
(stale value served, failure held 30 s). The 30 `cached("training",
get_today_metrics)` sites need no change. Do not `isinstance(x, dict)`-branch
on it expecting a fresh dict to mutate; `cached()` returns a copy anyway.

## 1.4 — nothing to do in app.py

## 2.1 purge keeps rider input — nothing to do in app.py

`db.purge_profile_data` parks RPE / rpe_at / rpe_scale / ftp_test_review /
is_race in `<profile>/rider_input.json` (keyed by intervals.icu id) before it
deletes, and keeps a wellness row whose HRV the rider typed. They come back
via `persist_icu_activity` and `db.sync_activities`. Helpers, if Wave 2 needs
them: `ride_storage.stashed_rider_input(ids, keys)`,
`ride_storage.forget_stashed_rider_input(restored)`,
`ride_storage.RIDER_RIDE_KEYS`.

## 2.2 one writer path for ride records — app.py writers to move

```python
ride_storage.update_ride(ride_id, mutator) -> dict | None
# ride_id: "icu_<id>", a bare ICU id, or "fit_<stem>" (the FIT's load sidecar).
# mutator(record) -> record to write, or None to write nothing. Runs under the
# record's lock on a fresh read: keep it quick -- fetch/compute BEFORE, merge
# inside. Never called for a missing record (returns None -> answer 404).
ride_storage.update_json_record(path, mutator)   # same, for an explicit path;
# mutator gets None for a missing file (use it to create)
```

Writers to move (each currently does its own read + `write_text`, unlocked):

| where | now | change to |
|---|---|---|
| app.py `api_ride_rpe` ICU branch (~18060-18076) | read, set/pop rpe keys, `path.write_text` | `rec = _rs.update_ride(ride_id, set_or_clear)`; `rec is None` -> 404 |
| app.py `api_ride_prs` lazy persist (~18107-18112) | `path.write_text(rec)` from a copy read before the compute | `_rs.update_ride(ext, lambda r: {**r, "prs": prs})` |
| app.py `api_ride_ftp_test_review` (~19228-19238) | `get_icu_ride` copy + `path.write_text` | `_rs.update_ride(ride_id, lambda r: {**r, "ftp_test_review": {...}})` |
| app.py `_augment_icu_record_with_dfa` write-back (~17577) | writes the WHOLE record read before up to 45 s of fetches | build a dict of only the keys it sets (dfa_*, rr_intervals_count, dfa_timeout_count, dfa_algo_version) and `_rs.update_ride(external_id, lambda r: {**r, **dfa_keys})` |
| power_curve.py `_mark_no_streams` (~683/685) | `_atomic_write_json(ride_path, data)` of a copy | `ride_storage.update_json_record(ride_path, lambda r: None if r is None else {**r, "no_streams_available": True, "streams_fetch_failed_at": ts})` inside the same sync gate |
| power_curve.py backfill write (~963/965) | `_atomic_write_json(ride_path, data)` of a copy held across the fetch | merge only efforts / streams / cleared markers / FTP-test detection keys through `update_json_record` |

`set_fit_rpe` (the `fit_` branch of api_ride_rpe) is already on the locked
path. `persist_icu_activity`, `backfill_icu_sports_from_db`,
`_persist_rider_input` and `write_fit_load_sidecar` are moved.

## 2.3 — nothing to do in app.py

`db._upsert_icu_metric` is private to the sync. `/api/metrics/log` keeps
`db.log_metric` (the rider's own write).

## 3.1 / 3.2 — nothing to do in app.py

`ProfileManager._write_json` / `_write_text_atomic` / `_load_json` are fixed
at the helper; every profile JSON goes through them.

## 5.2 local CTL/ATL and the week-activity collector

`fitness.state()` is fixed (same ride list for both EWMAs).
`ride_storage.compute_local_ctl(days=90, tau=42, rides=None, today=None)`.

The collector: app.py `_collect_week_activities` (~13038-13138). Replace

```python
    try:
        from ride_storage import list_rides
        for r in list_rides():
            _add(r)
```

with

```python
    try:
        from ride_storage import activity_rows_between
        for r in activity_rows_between(current_week.start, today + timedelta(days=1)):
            _add(r)
```

`activity_rows_between(start, end)` returns rides from `load_all_rides()`
(ICU records + FIT imports, deduped) with day in [start, end), shaped as
`_add` reads them: `date, start_date_local, tss, duration_min (moving),
intensity_factor, sport, id`. The `query_activities` loop above it may stay
(the collector's own dedupe on start time + duration merges the ICU copy);
`_add` still applies its own upper bound. The same `list_rides()` import is
also at app.py ~15421 — check whether that reader wants the same rows.

## 5.3 cache scope — nothing required; three bypasses to know about

`cache.cached` now stores a fill only if no `clear_cache()` ran during it,
and an entry is a hit only under the active profile it was filled for
(`profile_manager` registers `cache.set_scope` at import). app.py reads and
writes `_cache` directly, bypassing both guards, at:
`banister_oos_default` (~1916-1942), `power_curve_{pid}_...` (~1974-2053),
`fatigue_resistance_{pid}_...` (~2536-2658). The last two carry the profile
id in the key; `banister_oos_default` does not — either put `pm.active_id`
in that key or route it through `cached()`.

## 5.4 sync restart — status field for the UI

`db.get_sync_status()` has a new key `loop_alive` (bool). `/api/sync/status`
(app.py ~8590) returns it already; the dashboard can show "sync stopped"
when it is False and the profile has credentials. `restart_sync()` no longer
refuses to start; no call-site change.

## 5.5 `sportInfo` — one helper; readers to switch

```python
training.cycling_sport_info(wellness_record) -> dict
# the first sportInfo entry whose type is Ride / VirtualRide; a record whose
# entries carry no "type" at all reads entry [0] (older payloads, fixtures);
# otherwise {}.
```

Switched: `db.sync_wellness` (wellness.eftp, eFTP/W'/Pmax metrics),
`ride_storage.latest_eftp`. Still reading `sportInfo[0]` (not my files):

- app.py ~1023, ~3064, ~4671, ~8007 (the athlete-numbers eFTP prefill),
  ~8366, ~8779, ~8843, ~16524
- app.py ~4769 BUILDS a synthetic `sportInfo: [{"eftp": ...}]` (no type) —
  reads correctly through the helper's no-type rule
- training_planner.py ~15818 (the 7-day eFTP drift streak)

Each becomes `ci = cycling_sport_info(w)` and `ci.get("eftp")` /
`ci.get("wPrime")` / `ci.get("pMax")`.
