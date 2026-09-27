"""G3 (contract 10.6): the plan is written by plan_store alone -- seen at
run time, on every route.

tests/plan_route_sweep.py drives every route of the app (with a body, and
the plan state a conditional writer needs), the cron sequence of the stack's
morning adapter, the background hooks, a profile switch and four boots (a
legacy home's migration, a legacy root plan in a home of several profiles,
restore from a backup, the stale-classification rewrite), recording every
file operation under a plan dir with its stack. A write is owned when the
innermost src/ frame is plan_store.py.

UNOWNED is what 433c6067 wrote outside the store: the round's worklist, keyed
"route | door | innermost src function". It may only shrink and ends empty.
The sweep's self-test is the audit's plants: a write through the store's
private writer, a raw write, a stale copy through the door, a write inside a
mutator -- each flagged or refused -- and a clean write through the door
(the negative control) that must not be.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

# Non-GET routes that write the plan: each must reach its write in the sweep.
WRITERS = {
    "POST /api/plan/accept-redraw", "POST /api/plan/add-race", "POST /api/plan/auto-adjust",
    "POST /api/plan/auto-recalc", "POST /api/plan/continuous/deload-revert",
    "POST /api/plan/dismiss-session", "POST /api/plan/ftp-test-type", "POST /api/plan/generate",
    "POST /api/plan/mark-unavailable", "POST /api/plan/move-session", "POST /api/plan/re-draw",
    "POST /api/plan/reforecast", "POST /api/plan/regenerate", "POST /api/plan/rematch",
    "POST /api/plan/save-availability", "POST /api/plan/swap-type", "POST /api/plan/update",
    "POST /api/readiness/apply-tier-down", "POST /api/readiness/revert-cap",
    "POST /api/today-session/persist", "POST /api/rides/sync", "POST /api/ride/import",
    "DELETE /api/profiles/{profile_id}",
}

# Non-GET routes that do not write the plan, and why.
NOOP = {
    "POST /api/setup/test-icu": "checks intervals.icu credentials",
    "POST /api/setup/check-activities": "counts the rider's intervals.icu activities",
    "POST /api/setup/save": "onboarding preferences and credentials",
    "POST /api/profiles/create": "a new profile has no plan until it generates one",
    "POST /api/profiles/switch": "changes which profile a request captures, not a plan",
    "PUT /api/profiles/{profile_id}": "renames a profile",
    "POST /api/profile/update-ftp": "the FTP is the profile's; sessions are relative to it",
    "POST /api/profile/backfill-history": "ride analytics backfill",
    "POST /api/profile/dfa-backfill": "ride analytics backfill",
    "POST /api/profile/dfa-backfill/cancel": "cancels a backfill task (422 without task_id)",
    "POST /api/activity/{activity_id}/race": "flags an intervals.icu activity (404 for the probe id)",
    "POST /api/wellness/import-hrv4training": "wellness store",
    "POST /api/wellness/manual-hrv": "wellness store",
    "POST /api/wellness/backfill-3d-fitness": "wellness store",
    "POST /api/wellness/hrv-recording-dismiss": "wellness UI state (400 without an id)",
    "POST /api/workouts/bulk-segments": "workout library",
    "POST /api/routes/suggest": "route library",
    "POST /api/icu/disconnect": "credentials",
    "POST /api/icu/push": "mirrors the stored plan to intervals.icu (reads it)",
    "POST /api/calendar/push-workout": "pushes a library workout to the intervals.icu calendar",
    "POST /api/settings": "preferences",
    "POST /api/settings/zones": "zone preferences (400 for the probe body)",
    "POST /api/sync": "pulls intervals.icu into the SQLite mirror; the plan adapts on /api/rides/sync",
    "POST /api/metrics/log": "metrics store",
    "POST /api/daily-log": "the morning questionnaire (10.5 moves the readiness adjustment here)",
    "POST /api/retest-nudge/snooze": "preferences",
    "POST /api/plan/preview-redraw": "a preview: answers a draft, writes nothing",
    "POST /api/ride/{ride_id}/rpe": "ride sidecar (404 for the probe id)",
    "POST /api/ride/{ride_id}/ftp-test-review": "FTP review (400 for the probe id)",
    "POST /api/profile/pr-toast-queue/drain": "UI queue",
    "POST /api/icu/lazy-sync": "kicks the background intervals.icu pull (offline here)",
    "DELETE /api/rides/{ride_id}": "deletes a ride (404 for the probe id); the plan adapts on sync",
    "POST /api/diag/frontend-error": "log line",
}

# The worklist: writes outside plan_store, "route | door | innermost src
# function". 98 at 433c6067; what is left.
UNOWNED = {
    'POST /api/plan/generate (seed) | none | export_plan_md',
    'POST /api/plan/generate | none | export_plan_md',
    'switch: B generate | none | export_plan_md',
}

PLANTS = {"POST /api/zz_plant_direct", "POST /api/zz_plant_raw", "POST /api/zz_plant_nested"}


@pytest.fixture(scope="module")
def sweep(tmp_path_factory):
    out = tmp_path_factory.mktemp("sweep") / "sweep.json"
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    r = subprocess.run([sys.executable, str(TESTS / "plan_route_sweep.py"), str(out), "--plant"],
                       env=env, capture_output=True, text=True, timeout=600)
    assert out.exists(), r.stdout[-3000:] + r.stderr[-3000:]
    d = json.loads(out.read_text())
    assert d.get("seed_generate") == [200, "ok"], d.get("seed_generate")
    return d


def _innermost(rec) -> str:
    return rec["stack"][0].split(":")[-1] if rec["stack"] else "?"


def _unowned(d) -> set:
    return {f"{r['route']} | {r['door']} | {_innermost(r)}" for r in d["records"]
            if r["door"] != "store" and not r["route"].startswith("POST /api/zz_plant")}


def _calls(d) -> dict:
    return {c["route"]: c for c in d["calls"]}


def test_every_write_is_the_stores(sweep):
    new = _unowned(sweep) - UNOWNED
    assert not new, "written outside plan_store:\n  " + "\n  ".join(sorted(new))


def test_the_worklist_only_shrinks(sweep):
    stale = UNOWNED - _unowned(sweep)
    assert not stale, "fixed -- delete from UNOWNED:\n  " + "\n  ".join(sorted(stale))


def test_every_route_is_classified(sweep):
    routes = {f"{m} {r['path']}" for r in sweep["routes"] for m in r["methods"]
              if m not in ("GET", "HEAD")}
    assert routes - WRITERS - set(NOOP) == set(), "classify these routes (WRITERS or NOOP)"
    assert (WRITERS | set(NOOP)) - routes == set(), "these routes are gone"


def test_every_writer_reaches_its_write(sweep):
    calls = _calls(sweep)
    missed = {r: (calls[r]["status"], calls[r]["writes"]) for r in WRITERS
              if not (isinstance(calls[r]["status"], int) and calls[r]["status"] < 400
                      and calls[r]["writes"] > 0)}
    assert not missed, missed


def test_the_no_op_routes_write_nothing(sweep):
    calls = _calls(sweep)
    wrote = {r: calls[r]["writes"] for r in NOOP if calls[r]["writes"]}
    assert not wrote, wrote


def test_no_read_writes(sweep):
    wrote = [(c["route"], c["writes"], c["plan_changed"]) for c in sweep["calls"]
             if c["route"].startswith("GET ") and (c["writes"] or c["plan_changed"])]
    assert not wrote, wrote


def test_no_route_errors(sweep):
    bad = [(c["route"], c["status"]) for c in sweep["calls"]
           if not c["route"].startswith("POST /api/zz_plant")
           and (str(c["status"]).startswith("EXC") or c["status"] == 500)]
    assert not bad, bad


def test_nothing_writes_a_stale_copy_through_the_door(sweep):
    stale = [m for m in sweep["mutators"] if m["stale"] and not m["route"].startswith("POST /api/zz_plant")]
    assert not stale, stale


def test_no_response_enrichment_is_persisted(sweep):
    assert [c["route"] for c in sweep["calls"] if c.get("enrichment_persisted")] == []


def test_the_boots_and_the_switch(sweep):
    assert sweep["boots"]["boot0_migrated"]
    assert sweep["boots"]["boot3_restored"]
    assert sweep["switch"]["a_unchanged"] and sweep["switch"]["b_has_plan"]


# ── the self-test ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_the_planted_writers_are_flagged_or_refused(sweep, plant):
    c = _calls(sweep)[plant]
    flagged = any(r["route"] == plant and r["door"] != "store" for r in sweep["records"])
    refused = (c["status"] == 500 or str(c["status"]).startswith("EXC PlanStoreError")) \
        and not c["plan_changed"]
    assert flagged or refused, c


def test_the_planted_stale_copy_is_flagged(sweep):
    assert any(m["route"] == "POST /api/zz_plant_stale" and m["stale"] for m in sweep["mutators"])


def test_the_negative_control_is_owned(sweep):
    c = _calls(sweep)["POST /api/zz_plant_ok"]
    recs = [r for r in sweep["records"] if r["route"] == "POST /api/zz_plant_ok"]
    assert c["status"] == 200 and c["plan_changed"] and recs
    if "plan_store.py" in " ".join(" ".join(r["stack"]) for r in recs):
        assert all(r["door"] == "store" for r in recs), recs
