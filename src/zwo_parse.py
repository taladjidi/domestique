"""Reading a .zwo file: what is in it, measured, with no planner state.

Everything here takes a parsed ``<workout>`` element (or a series of power
samples) and returns numbers. No module of the app is imported, nothing is
cached, and no rider, plan or library row is visible from here -- which is the
point: these are the measurements the planner, the library index and the
tests all have to agree on, and they were previously spread through a
16,000-line module where agreeing was a matter of trust.

The microinterval measure is the literature's, not a heuristic: see
``micro_shape`` for the protocol it comes from and the constants it turns.
"""
from __future__ import annotations

# What makes a bout a MICROinterval, from the literature (see micro_shape):
# 30 s and 40 s families, 15-45 s; a 60 s rep is the format they are contrasted
# against. Recovery at most 1.5x the bout -- 30/15 and 40/20 are 0.5, 30/30 is
# 1.0 -- because the short recovery IS the mechanism. At or over FTP, and under
# the line past which a 30/15 reads neuromuscular (the generator draws it at
# the same 1.50).
MICRO_MAX_BOUT_S = 40
MICRO_MAX_REST_RATIO = 1.5
MICRO_MIN_POWER = 1.00
MICRO_MAX_POWER = 1.50
# The recovery leg must actually recover. Ronnestad's OFF leg is ~60 % of
# 40-min maximal power; every unambiguous file in the library rests at or
# under 0.70. Without a ceiling here an over-under -- 30 s at 1.15 alternating
# with 30 s at 0.95, which never lets oxygen uptake fall -- reads as a series
# of microintervals, because its "recovery" is merely under FTP.
MICRO_MAX_REST_POWER = 0.80
# What COMPETES with the short work: a sustained bout longer than the ceiling,
# from tempo up. Not from FTP up -- a 4 x 6 min sweet-spot main set at 0.90
# never counts as work by an at-or-over-FTP test, so a sweet-spot session that
# happens to carry ten 15 s activations read as a microinterval session.
MICRO_LONG_MIN_POWER = 0.85
# A session must carry a full series (Ronnestad's own is 5-13 reps) and about
# one series' worth of short work, and that work must not be a sideshow beside
# a longer effort.
MICRO_MIN_SERIES_REPS = 5
MICRO_MIN_WORK_S = 300


def micro_shape(workout_el) -> tuple[int, int, int]:
    """(reps in the longest short-work series, seconds of short work, seconds
    of long work) for one parsed .zwo.

    THE DEFINITION, from the literature this family comes from. Ronnestad's
    protocol is series of 5-13 x (30 s ON / 15 s OFF), the ON leg at ~118 % of
    40-min maximal power, the OFF at ~60 % (PMC11680548); the wider
    short-interval literature runs 10-40 s bouts at ~100-130 % of maximal
    aerobic power with recovery at or under the work duration, and contrasts
    them against LONG intervals of 4-5 min. The mechanism is the short
    recovery: oxygen uptake does not fall between reps.

    So short work is a bout <= MICRO_MAX_BOUT_S at MICRO_MIN_POWER..
    MICRO_MAX_POWER whose recovery is at most MICRO_MAX_REST_RATIO x the bout
    and at most MICRO_MAX_REST_POWER (a leg at 0.95 is a second effort, not a
    recovery).
    Long work is anything from MICRO_LONG_MIN_POWER up in a bout longer than
    that ceiling.

    Files are authored three ways and all three are read: IntervalsT, repeated
    SteadyState pairs, and repeated work Ramps. Measuring only IntervalsT saw
    a 4x40s opener and missed the 6x4min main set beside it, which is how a
    three-minute-rep session came to read as a microinterval session.
    """
    micro_s = long_s = best = 0
    run = 0                  # reps in the series being counted right now
    consumed = -1            # index of the recovery leg the last bout used
    segs = list(workout_el)
    for i, el in enumerate(segs):
        tag = el.tag
        try:
            if tag == "IntervalsT":
                on = int(float(el.get("OnDuration", 0) or 0))
                off = int(float(el.get("OffDuration", 0) or 0))
                pw = float(el.get("OnPower", 0) or 0)
                n = int(float(el.get("Repeat", 0) or 0))
                if on > 0 and n > 0:
                    off_pw = float(el.get("OffPower", 0) or 0)
                    if (MICRO_MIN_POWER <= pw <= MICRO_MAX_POWER
                            and on <= MICRO_MAX_BOUT_S and off <= on * MICRO_MAX_REST_RATIO
                            and off_pw <= MICRO_MAX_REST_POWER):
                        micro_s += on * n
                        best = max(best, n)
                    elif pw >= MICRO_LONG_MIN_POWER and on > MICRO_MAX_BOUT_S:
                        long_s += on * n
                run = 0
                continue
            dur = int(float(el.get("Duration", 0) or 0))
            if tag == "SteadyState":
                pw = lo = hi = float(el.get("Power", 0) or 0)
            elif tag in ("Ramp", "Warmup", "Cooldown"):
                lo = float(el.get("PowerLow", 0) or 0)
                hi = float(el.get("PowerHigh", 0) or 0)
                pw = (lo + hi) / 2
            else:
                # FreeRide and friends: not work, and not a recovery leg
                # either, so whatever series was running has ended.
                run = 0
                continue
        except (TypeError, ValueError):
            run = 0
            continue
        if dur <= 0:
            continue
        if MICRO_MIN_POWER <= pw <= MICRO_MAX_POWER and dur <= MICRO_MAX_BOUT_S:
            nxt = segs[i + 1] if i + 1 < len(segs) else None
            rest = rest_pw = 0
            if nxt is not None and nxt.tag == "SteadyState":
                try:
                    rest = int(float(nxt.get("Duration", 0) or 0))
                    rest_pw = float(nxt.get("Power", 0) or 0)
                except (TypeError, ValueError):
                    rest = 0
            if rest and rest <= dur * MICRO_MAX_REST_RATIO and rest_pw <= MICRO_MAX_REST_POWER:
                micro_s += dur
                run += 1
                best = max(best, run)
                consumed = i + 1
            else:
                # A series' LAST rep is followed by the series rest, not by the
                # short one: counting only bouts with a short recovery read
                # Ronnestad's 13 as 12 and dropped a 5-rep set to 4.
                if run > 0:
                    micro_s += dur
                    run += 1
                    best = max(best, run)
                run = 0
            continue
        # Sustained work, integrated rather than averaged: a 15-min ramp from
        # 0.5 to 1.0 spends its last 270 s at or over the line, and scoring it
        # at its 0.75 mean hid a sweet-spot pyramid behind eight 40/20s.
        if max(lo, hi) >= MICRO_LONG_MIN_POWER:
            if lo == hi:
                above = dur
            else:
                frac = (max(lo, hi) - MICRO_LONG_MIN_POWER) / abs(hi - lo)
                above = int(dur * max(0.0, min(1.0, frac)))
            if above > MICRO_MAX_BOUT_S:
                long_s += above
        # Anything that is not a short bout, and not the recovery leg one of
        # them just consumed, ends the series: two sets of four 30/15s with a
        # three-minute rest between them are two sets, not one series of
        # eight.
        if i != consumed:
            run = 0
    return best, micro_s, long_s

def rules_fingerprint() -> str:
    """The thresholds that decide a cached row's computed verdicts.

    The index stores conclusions, not just parsed facts, so an index built
    under other thresholds is wrong even though every .zwo file is untouched
    (count and mtime, which validate the rest of the header, cannot see it).
    Editing a constant below therefore invalidates the index by itself."""
    return "|".join(str(v) for v in (
        MICRO_MAX_BOUT_S, MICRO_MAX_REST_RATIO, MICRO_MAX_REST_POWER,
        MICRO_MIN_POWER, MICRO_MAX_POWER, MICRO_LONG_MIN_POWER,
        MICRO_MIN_SERIES_REPS, MICRO_MIN_WORK_S))

def is_microinterval(reps: int, micro_s: int, long_s: int) -> bool:
    """The verdict, from a file's shape (see micro_shape): a full series of
    short bouts, about a series' worth of work, and that work not a sideshow
    beside a longer effort. One definition, used by the index builder and by
    anything that asks."""
    return (reps >= MICRO_MIN_SERIES_REPS and micro_s >= MICRO_MIN_WORK_S
            and micro_s >= long_s)

def np_fraction_from_samples(samples: "list[float]") -> float:
    """Coggan NP as an FTP fraction over a 1 Hz planned-power series.

    30-s simple rolling mean, full windows only (first value covers samples
    [0, 30) — no zero-pad, no expanding mean), 4th-power mean, 4th root.
    Matches ride_storage.compute_power_tss windowing exactly, so planned
    library TSS and ridden TSS are in the same units (README documents the
    NP formula; the pre-v3.5.0 parser computed RMS power instead).
    Returns 0.0 for series shorter than one window (NP undefined).
    """
    n = len(samples)
    if n < 30:
        return 0.0
    s = sum(samples[:30])
    p4 = (s / 30.0) ** 4
    count = 1
    for i in range(30, n):
        s += samples[i] - samples[i - 30]
        p4 += (s / 30.0) ** 4
        count += 1
    return (p4 / count) ** 0.25
