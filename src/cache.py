"""The shared response cache.

Lifted out of app.py, where it sat inside the setup-wizard section while being
read from ten other sections and mutated directly -- past its own API -- from
four of them. That made it an invisible channel between every would-be module,
and it is why the substrate has to move before any feature area can.

`clear_cache()` keeps a registry of extra clearers rather than naming them.
The fatigue-resistance memo cannot live here: it forward-references profile
resolution, which would drag the profile machinery into the substrate and
defeat the point. app.py registers it instead.
"""
import threading
import time

from obs import error_codes, _log_error

# ── Cache ─────────────────────────────────────────────────────────────────────

_cache = {}
_cache_ts = {}
# Which profile each entry was filled under, and a generation that
# clear_cache() bumps. The audit (2026-09-23, data #5) started the ride
# archive fill for Alice, switched to Bob while it ran, and the fill landed
# AFTER the switch's clear_cache: Bob was served Alice's rides and CTL for
# five minutes. A fill now stores only if no clear ran meanwhile, and an
# entry is a hit only under the profile it was filled for.
_cache_scope = {}
_generation = 0
_generation_lock = threading.Lock()
_scope_fn = None


def set_scope(fn) -> None:
    """Register the function naming the current scope (the active profile
    id). profile_manager registers it; the substrate never imports it."""
    global _scope_fn
    _scope_fn = fn


def _scope():
    try:
        return _scope_fn() if _scope_fn is not None else None
    except Exception:  # noqa: BLE001 — no scope is a scope, never a failure
        return None


def _fresh(key, now, ttl, scope) -> bool:
    return (key in _cache and now - _cache_ts.get(key, 0) < ttl
            and _cache_scope.get(key) == scope)


def _defensive_copy(value):
    """Return a shallow copy, so a caller cannot mutate what the cache serves.

    `cached()` used to hand every caller the SAME object. One of them mutates
    it -- app.py's readiness endpoint writes severity/source/severity_reasons
    into the dict it was handed -- so those keys were baked into the cache for
    every subsequent reader. More generally it meant clear_cache() did not
    really invalidate: callers went on holding, and mutating, what had been
    the cache's object.

    Shallow, not deep, and deliberately so. The observed failure is a
    top-level key assignment; nothing mutates nested structures. Deep-copying
    would prevent a bug that does not exist, at the cost of copying a 17 MB
    ride archive on every one of the twenty-two `all_rides` reads per request.
    A caller that needs to mutate something nested copies it itself.
    """
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    return value


class Unavailable(dict):
    """An empty answer that means "could not ask", not "there is nothing".

    A fill function returns this instead of raising when its own callers
    need an empty dict (training.get_today_metrics: the planner calls it
    directly and tests `if not metrics`). The cache treats it as a failed
    fill -- stale value served, failure held 30 s -- where a plain `{}` is
    stored as the answer. Without it, one intervals.icu blip replaced the
    last good CTL/ATL with `{}` for the full 300 s TTL (audit 2026-09-23,
    integ #3).
    """


class _FillFailed(Exception):
    pass


_locks: dict = {}
_locks_guard = threading.Lock()


def _lock_for(key):
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def cached(key, fn, ttl=300):
    now = time.time()
    if _fresh(key, now, ttl, _scope()):
        return _defensive_copy(_cache[key])
    # Single flight per key. Without it a cold home page fired four
    # concurrent archive parses through this function, 17 s of CPU, every
    # request waiting for its own copy (the audit's performance lens,
    # 2026-09-14). The second thread waits for the first and reads its result.
    with _lock_for(key):
        return _fill(key, fn, ttl)


def _fill(key, fn, ttl):
    now = time.time()
    scope, generation = _scope(), _generation
    if _fresh(key, now, ttl, scope):
        return _defensive_copy(_cache[key])
    try:
        result = fn()
        if isinstance(result, Unavailable):
            raise _FillFailed(f"{key}: the fill could not reach its source")
    except Exception as e:
        # If API call fails (no internet, DNS error), return stale cache --
        # this profile's, never another's.
        if key in _cache and _cache_scope.get(key) == scope:
            return _defensive_copy(_cache[key])
        # v1.6.0 — log under E_CACHE_<key>-or-GENERIC and stick the empty
        # result for only 30s so transient errors don't sit in cache for
        # the full ttl. Trick: backdate _cache_ts to (now - (ttl - 30))
        # so the staleness check ``now - ts < ttl`` flips back to False
        # after 30 wall-clock seconds.
        cache_code = {
            "training": error_codes.Codes.CACHE_TRAINING,
            "sleep": error_codes.Codes.CACHE_SLEEP,
            "wellness": error_codes.Codes.CACHE_WELLNESS,
        }.get(key, error_codes.Codes.CACHE_GENERIC)
        _log_error(cache_code, exc=e, cache_key=key)
        if _still_current(scope, generation):
            _cache[key] = {}
            _cache_scope[key] = scope
            if ttl > 30:
                _cache_ts[key] = now - (ttl - 30)
            else:
                _cache_ts[key] = now
        return {}
    if _still_current(scope, generation):
        _cache[key] = result
        _cache_ts[key] = now
        _cache_scope[key] = scope
    # else: a clear_cache() or a profile switch landed while fn() ran, so the
    # result describes what was true before it. The caller that asked still
    # gets it; nobody after it does.
    return _defensive_copy(result)


def _still_current(scope, generation) -> bool:
    return generation == _generation and scope == _scope()

_extra_clearers: list = []


def register_clearer(fn) -> None:
    """Register a callable to run on clear_cache().

    For caches that cannot live in this module -- anything keyed on state
    the substrate must not know about. Keeps clear_cache() from importing
    the things it clears.
    """
    _extra_clearers.append(fn)


def clear_cache():
    global _generation
    with _generation_lock:
        _generation += 1
    _cache.clear()
    _cache_ts.clear()
    _cache_scope.clear()
    # AC2c: the fatigue-resistance memo is keyed on (ride_id, ftp, window, kj)
    # only — NOT profile — so a profile switch (clear_cache is an on_switch
    # callback) must drop it or profile B could serve A's cached curve.
    for fn in tuple(_extra_clearers):
        try:
            fn()
        except Exception:
            # A clearer that raises must not leave the rest uncleared.
            pass
