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


def test_a_partial_answer_is_served_but_not_held_for_the_full_ttl(icu, monkeypatch):
    """Review finding: wellness answered but the activities fetch failed ->
    `recent_activities: []` was cached for the full 300 s."""
    def activities_down(days=7):
        raise tr.ICUNetworkError("blip")
    monkeypatch.setattr(tr, "fetch_activities", activities_down)
    got = _read()
    assert got["ctl"] == 60.0 and got["recent_activities"] == []
    left = 300 - (time.time() - cache._cache_ts["training"])
    assert left <= 30.5, f"a partial answer was held for {left:.0f}s"


def test_an_outage_asks_intervals_once_per_hold_not_once_per_read(icu, monkeypatch):
    """Fix-forward F1: the stale-served failure recorded nothing, so every
    read during an outage asked intervals.icu again -- three reforecasts
    behind a 2 s-failing fetch took 12.1 s and asked 6x (slim/6: 2.2 s, 1x,
    because it cached its `{}` for the TTL). The failure now holds the last
    good value for a TTL window: one ask per window, like slim/6."""
    asks = {"n": 0}
    inner = tr.fetch_wellness

    def counting(days=42):
        asks["n"] += 1
        return inner(days)
    monkeypatch.setattr(tr, "fetch_wellness", counting)
    assert _read()["ctl"] == 60.0
    cache._cache_ts["training"] -= 301           # expired
    icu["up"] = False
    asks["n"] = 0
    got = [_read().get("ctl") for _ in range(5)]
    import threading
    ts = [threading.Thread(target=lambda: got.append(_read().get("ctl"))) for _ in range(4)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert got == [60.0] * 9, got
    assert asks["n"] == 1, f"intervals.icu asked {asks['n']}x during one outage window"
    cache._cache_ts["training"] -= 301           # the hold ends: ask again
    icu["up"] = True
    assert _read()["ctl"] == 60.0 and asks["n"] == 2


def test_after_a_cold_outage_recovery_shows_within_30_s(icu, monkeypatch):
    """Review of the F1 fix: the stale-value hold also caught the `{}` a
    cold failure stores, so a second failure re-held it for 300 s and the
    dashboard stayed blank for five minutes after intervals.icu came back.
    A failure placeholder, and a Partial, keep the 30 s hold."""
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(cache.time, "time", lambda: now["t"])
    icu["up"] = False
    assert not _read()                    # t=0: cold, down
    now["t"] += 31
    assert not _read()                    # t=31: still down, asked again
    now["t"] += 31
    icu["up"] = True
    assert _read().get("ctl") == 60.0, "recovery hidden behind a re-held failure"


def test_a_stale_partial_is_re_held_for_30_s_not_the_ttl(icu, monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(cache.time, "time", lambda: now["t"])

    def activities_down(days=7):
        raise tr.ICUNetworkError("blip")
    monkeypatch.setattr(tr, "fetch_activities", activities_down)
    assert _read()["ctl"] == 60.0         # a Partial
    now["t"] += 31
    icu["up"] = False
    assert _read()["ctl"] == 60.0         # served stale
    left = 300 - (now["t"] - cache._cache_ts["training"])
    assert left <= 30.5, f"a stale Partial was re-held for {left:.0f}s"
