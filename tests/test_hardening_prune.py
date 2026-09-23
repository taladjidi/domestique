"""An empty or failed answer from intervals.icu is not a fact (contract 1.1).

The audit (2026-09-23, integ #2) reproduced it: one 200 answer of `[]` from
the activities endpoint was read as "the rider deleted every ride upstream".
The pruner's only guard was a flat cap of 20, which a small archive fits
under, so 18 rides over 80 days went to 0 -- and with them `is_race`, the DFA
results and the rider's RPE, none of which a re-sync can bring back. A `null`
body was turned into `[]` by fetch_activities and did the same.

The rule now lives inside prune_deleted_icu_records, so it holds whatever a
caller passes: a prune needs a non-empty answer that overlaps the local rides
in its window, and never removes more than half of them in one pass.
"""
from __future__ import annotations

import datetime
import json
import time
import types
from unittest import mock

import pytest

import config
import db
import ride_storage as rs
import training as tr
import app as app_module
from profile_manager import ProfileManager


# ── the pruner on its own ────────────────────────────────────────────────────

@pytest.fixture()
def icu_dir(tmp_path, monkeypatch):
    d = tmp_path / "rides" / "icu"
    d.mkdir(parents=True)
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: d)
    return d


def _rec(icu_dir, rid, days_ago):
    day = (datetime.date.today() - datetime.timedelta(days=days_ago)).isoformat()
    (icu_dir / f"{rid}.json").write_text(json.dumps({
        "ride_id": f"icu_{rid}", "source": "icu", "name": "Ride",
        "started_at": f"{day}T10:00:00", "tss": 60, "duration_s": 3600,
    }), encoding="utf-8")


def _five(icu_dir):
    ids = [f"i{100 + k}" for k in range(5)]
    for k, rid in enumerate(ids):
        _rec(icu_dir, rid, days_ago=k + 1)
    return ids


@pytest.mark.parametrize("answer", [set(), None, [], "", {"id": "i1"}, 42],
                         ids=["empty-set", "None", "empty-list", "empty-str",
                              "dict", "int"])
def test_an_empty_or_malformed_answer_prunes_nothing(icu_dir, answer):
    """Five rides in the window: small enough for the old flat cap of 20."""
    _five(icu_dir)
    assert rs.prune_deleted_icu_records(answer, window_days=90) == 0
    assert len(list(icu_dir.glob("*.json"))) == 5


def test_an_answer_that_shares_no_ride_with_the_window_prunes_nothing(icu_dir):
    """A non-empty answer made only of ids we have never seen says nothing
    about the rides we have: it is someone else's list, or a broken one."""
    _five(icu_dir)
    assert rs.prune_deleted_icu_records({"i999", "i998"}, window_days=90) == 0
    assert len(list(icu_dir.glob("*.json"))) == 5


def test_more_than_half_the_window_missing_prunes_nothing(icu_dir):
    ids = _five(icu_dir)
    # 3 of 5 missing: more than half -- a broken answer, not a rider's cleanup
    assert rs.prune_deleted_icu_records(set(ids[:2]), window_days=90) == 0
    assert len(list(icu_dir.glob("*.json"))) == 5


def test_a_real_upstream_deletion_is_still_pruned(icu_dir):
    ids = _five(icu_dir)
    assert rs.prune_deleted_icu_records(set(ids[1:]), window_days=90) == 1
    assert not (icu_dir / f"{ids[0]}.json").exists()
    # half exactly is still believable (the Karoo + Garmin double upload)
    _rec(icu_dir, "iA", 2)
    _rec(icu_dir, "iB", 2)
    for rid in ids[1:]:
        (icu_dir / f"{rid}.json").unlink()
    assert rs.prune_deleted_icu_records({"iA"}, window_days=90) == 1


# ── the fetch no longer turns a failure into [] ──────────────────────────────

@pytest.fixture()
def creds(monkeypatch):
    monkeypatch.setattr(config, "ICU_ATHLETE_ID", "i1", raising=False)
    monkeypatch.setattr(config, "ICU_API_KEY", "k", raising=False)


@pytest.mark.parametrize("body", [None, {"error": "x"}, "oops", 7],
                         ids=["null", "dict", "str", "int"])
def test_fetch_activities_raises_on_a_body_that_is_not_a_list(creds, body):
    with mock.patch.object(tr, "_get", lambda path, params=None: body):
        with pytest.raises(tr.ICUServerError):
            tr.fetch_activities(days=7)


def test_fetch_activities_passes_a_real_empty_list_through(creds):
    with mock.patch.object(tr, "_get", lambda path, params=None: []):
        assert tr.fetch_activities(days=7) == []


# ── the whole sync pass (the audit's reproduction, ported) ───────────────────

@pytest.fixture
def env(tmp_path):
    p = mock.patch("pathlib.Path.home", return_value=tmp_path)
    p.start()
    old = ProfileManager._instance
    ProfileManager._instance = None
    old_db = db.DB_PATH
    old_data = app_module.DATA_DIR
    app_module.DATA_DIR = tmp_path / ".domestique"
    pm = ProfileManager.get()
    a = pm.create_profile("Alpha")
    pm.switch(a)
    db._sync_stop.clear()
    config.ICU_ATHLETE_ID = "i1"
    config.ICU_API_KEY = "k"
    yield pm
    p.stop()
    ProfileManager._instance = old
    for k in ("ICU_ATHLETE_ID", "ICU_API_KEY"):
        try:
            delattr(config, k)
        except AttributeError:
            pass
    app_module.DATA_DIR = old_data
    db.set_db_path(old_db)
    app_module.clear_cache()


def _acts(n, span_days):
    out = []
    for i in range(n):
        d = datetime.date.today() - datetime.timedelta(days=(i * span_days) // max(n, 1))
        out.append({"id": f"i{1000 + i}", "type": "Ride",
                    "start_date_local": f"{d}T08:00:00", "moving_time": 3600,
                    "icu_training_load": 60, "average_watts": 200,
                    "distance": 30000})
    return out


def _sync_pass(n, span, last_sync_days_ago, answer):
    acts = _acts(n, span)
    for a in acts:
        rs.persist_icu_activity(dict(a))
    app_module._write_last_sync_at(time.time() - last_sync_days_ago * 86400)
    before = len(list(rs._icu_rides_dir().glob("*.json")))
    with mock.patch.object(tr, "_get", lambda path, params=None: answer(acts)), \
         mock.patch.object(app_module, "_augment_icu_record_with_dfa",
                           lambda *a, **k: None):
        res = app_module._sync_icu_activities(force=False)
    after = len(list(rs._icu_rides_dir().glob("*.json")))
    return types.SimpleNamespace(before=before, after=after, status=res["status"])


@pytest.mark.parametrize("n,span,ago", [(5, 6, 1), (15, 20, 20), (25, 6, 1)])
@pytest.mark.parametrize("answer", [lambda acts: [], lambda acts: None],
                         ids=["200-empty", "200-null"])
def test_a_sync_pass_answered_empty_keeps_every_ride(env, n, span, ago, answer):
    r = _sync_pass(n, span, ago, answer)
    assert r.after == r.before == n, vars(r)


def test_a_sync_pass_answered_null_reports_a_failed_fetch(env):
    r = _sync_pass(5, 6, 1, lambda acts: None)
    assert r.status == "fetch_failed", vars(r)


def test_control_a_full_answer_keeps_every_ride(env):
    r = _sync_pass(5, 6, 1, lambda acts: acts)
    assert r.after == r.before == 5, vars(r)


def test_control_one_ride_really_deleted_upstream_is_pruned(env):
    r = _sync_pass(5, 6, 1, lambda acts: acts[1:])
    assert r.after == r.before - 1, vars(r)
