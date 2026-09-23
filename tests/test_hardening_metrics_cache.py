"""When intervals.icu fails, the training cache keeps its last good value
(contract 1.3).

The audit (2026-09-23, integ #3) expired a good `training` entry and let
intervals.icu fail once: get_today_metrics caught the error and returned
`{}`, the cache stored `{}` as a success for the full 300 s, and the good
value was thrown away. CTL, ATL and TSB read unknown for five minutes after a
one-second blip, although the cache has a stale-on-failure policy for
exactly this -- it never saw a failure.

get_today_metrics now answers cache.Unavailable() on an ICU failure: still an
empty dict for the planner's direct callers (`if not metrics`), but the cache
reads it as a failed fill.
"""
from __future__ import annotations

import time

import pytest

import cache
import config
import training as tr

GOOD = [{"id": "2026-09-23", "ctl": 60.0, "atl": 55.0, "sportInfo": []}]


def _drop_icu_creds():
    for k in ("ICU_ATHLETE_ID", "ICU_API_KEY"):
        try:
            delattr(config, k)
        except AttributeError:
            pass


@pytest.fixture()
def icu(monkeypatch, request):
    # Not monkeypatch.setattr: config resolves ICU_* through a module
    # __getattr__, so monkeypatch would record the RESOLVED value and restore
    # it as a real attribute, shadowing the resolver for every later test
    # (it made test_issue11's run_sync skip for want of an athlete id).
    config.ICU_ATHLETE_ID, config.ICU_API_KEY = "i1", "k"
    request.addfinalizer(_drop_icu_creds)
    state = {"up": True}

    def wellness(days=42):
        if state["up"]:
            return GOOD
        raise tr.ICUNetworkError("blip")
    monkeypatch.setattr(tr, "fetch_wellness", wellness)
    monkeypatch.setattr(tr, "fetch_activities", lambda days=7: [])
    cache.clear_cache()
    yield state
    cache.clear_cache()


def _read():
    return cache.cached("training", tr.get_today_metrics, ttl=300)


def test_a_blip_after_expiry_serves_the_last_good_value(icu):
    assert _read()["ctl"] == 60.0
    cache._cache_ts["training"] -= 301           # the entry has expired
    icu["up"] = False
    during = _read()
    assert during.get("ctl") == 60.0, f"blip served {during!r}, not the last good value"
    icu["up"] = True
    assert _read()["ctl"] == 60.0


def test_a_cold_blip_is_not_cached_for_the_full_ttl(icu):
    icu["up"] = False
    assert not _read()
    left = 300 - (time.time() - cache._cache_ts["training"])
    assert left <= 30.5, f"a failure was cached for {left:.0f}s"


def test_direct_callers_still_get_an_empty_dict_on_failure(icu):
    """training_planner calls get_today_metrics directly and tests `not m`."""
    icu["up"] = False
    m = tr.get_today_metrics()
    assert m == {} and not m
    assert isinstance(m, cache.Unavailable)


def test_control_no_credentials_is_an_answer_not_a_failure(monkeypatch):
    """No intervals.icu connection: `{}` is the true answer, cached as such."""
    monkeypatch.setattr(tr, "_require_credentials",
                        lambda: (_ for _ in ()).throw(tr.ICUCredentialsMissing("x")))
    m = tr.get_today_metrics()
    assert m == {} and not isinstance(m, cache.Unavailable)
