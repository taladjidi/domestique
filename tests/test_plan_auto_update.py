"""v1.8.24 — auto plan-update tier-dispatch (_apply_plan_update).

The shared core behind the manual "Update plan" button AND the ride-sync auto
path. Tier-dispatches:
  • current significant absence  → regenerate_from_today (recovery ramp)
  • otherwise                    → reforecast_dict (structure-preserving)

Guards under test (per /tmp/MASTER_DECISIONS_tasks12 §11):
  • RECENT-gap gate — an OLD recovered gap must NOT trigger a rebuild.
  • EPISODE latch — at most one rebuild per absence episode (no churn).
  • EVENT-TAPER guard — never silently rebuild inside the taper window.
  • Event target_date never moves; top-level plan keys survive a rebuild.
"""
from datetime import date, timedelta

import json
import pathlib
import pytest

import app
import training_planner as tp
import zwo_parse


T = date.today()


def test_auto_adapt_reconciles_current_week(monkeypatch):
    """v1.8.25 — the auto-adapt path (ride-sync / Update plan) now marks the
    current week's sessions done/missed from actuals BEFORE adapting, so a
    completed ride is reconciled automatically (no manual "Reconcile Week").
    """
    monday = T - timedelta(days=T.weekday())
    # current ISO week with a pending session today
    today_iso = T.isoformat()
    plan = _base_plan([{
        "week_num": 1, "start": monday.isoformat(),
        "end": (monday + timedelta(days=6)).isoformat(),
        "phase": "build", "tss_target": 300, "is_stepback": False,
        "sessions": [{
            "day": today_iso, "day_name": "X", "session_type": "z2",
            "duration_min": 60, "tss_estimate": 50, "status": "pending",
            "zwo_file": "endurance_steady_65pct_60min.zwo", "zwo_name": "E60",
            "description": "",
        }],
    }])
    # stub the matcher: today's session matches a real activity → done
    monkeypatch.setattr(app, "_collect_week_activities", lambda *a, **k: [{"id": 7}])
    monkeypatch.setattr(tp, "rematch_week", lambda *a, **k: {"matches": [{
        "session_date": today_iso, "new_status": "done", "activity_id": 7,
        "matched_axes": ["tss", "duration", "if"], "score": 0.95,
        "axes": {"tss": True}, "details": None,
    }], "summary": {}})

    pd, _action, _info, _status = _apply(plan, ctl=50, tsb=-5, activities=[{"id": 7}])
    sess = pd["weeks"][0]["sessions"][0]
    assert sess["status"] == "done", "auto-adapt did not reconcile the current week"
    assert sess.get("completion_matches"), "completion_matches not recorded on auto-reconcile"


def _mkweek(wn, start, tss,
            types=("z2", "z2", "rest", "threshold", "z2", "z2", "rest")):
    sessions = []
    for i, t in enumerate(types):
        d = start + timedelta(days=i)
        sessions.append({
            "day": d.isoformat(), "day_name": d.strftime("%a"),
            "session_type": t, "duration_min": 0 if t == "rest" else 60,
            "tss_estimate": 0 if t == "rest" else 50, "status": "pending",
            "description": "",
        })
    return {
        "week_num": wn, "start": start.isoformat(),
        "end": (start + timedelta(days=6)).isoformat(), "phase": "build",
        "tss_target": tss, "is_stepback": False, "sessions": sessions,
    }


def _base_plan(weeks, goal=None):
    return {
        "goal": goal or {
            "type": "general", "hours_per_week": 8, "max_weekday_hours": 2.0,
            "max_weekend_hours": 3.5, "rest_days": [2, 6], "longest_ride_h_90d": 2.0,
        },
        "availability": {}, "weeks": weeks,
    }


def _past_starts():
    """3 completed week-starts + the current week start (ends in the future)."""
    return (T - timedelta(days=21), T - timedelta(days=14),
            T - timedelta(days=7), T)


def _apply(plan, *, ctl, tsb, activities, debounce=True, today=T):
    return app._apply_plan_update(
        plan, training={"ctl": ctl, "tsb": tsb}, activities=activities,
        today=today, allow_regen=True, gap_debounce=debounce,
    )


def _full_adherence_activities(starts):
    acts = []
    for ws in starts:
        for i in range(7):
            acts.append({"date": (ws + timedelta(days=i)).isoformat(), "tss": 45})
    return acts


# ── tier dispatch ────────────────────────────────────────────────────────────

def test_trained_on_plan_rebalances_not_rebuilds():
    w1, w2, w3, w4 = _past_starts()
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300),
                       _mkweek(5, T + timedelta(days=7), 300)])
    acts = _full_adherence_activities([w1, w2, w3])
    _pd, action, _info, status = _apply(plan, ctl=50, tsb=-5, activities=acts)
    assert action == "rebalanced", status


def test_missed_two_plus_weeks_rebuilds_with_recovery_ramp():
    w1, w2, w3, w4 = _past_starts()
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300),
                       _mkweek(5, T + timedelta(days=7), 300),
                       _mkweek(6, T + timedelta(days=14), 300)])
    pd, action, info, status = _apply(plan, ctl=30, tsb=10, activities=[])
    assert action == "rebuilt", status
    assert info["regen_info"].get("recovery_ramp_weeks", 0) > 0
    assert "recovery ramp" in status.lower()


# ── episode latch (no churn) ─────────────────────────────────────────────────

def test_episode_latch_prevents_double_rebuild():
    w1, w2, w3, w4 = _past_starts()
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300),
                       _mkweek(5, T + timedelta(days=7), 300)])
    pd1, a1, _i, _s = _apply(plan, ctl=30, tsb=10, activities=[])
    assert a1 == "rebuilt"
    assert pd1.get("gap_regen_latch", {}).get("key")
    # Second pass over the just-rebuilt plan (same absence) must NOT rebuild.
    pd2, a2, _i2, _s2 = _apply(pd1, ctl=30, tsb=10, activities=[], debounce=True)
    assert a2 == "rebalanced", "latch failed — rebuilt twice for one absence"


def test_manual_path_also_latched_but_rebuild_from_scratch_is_the_force():
    """gap_debounce=False (if a caller ever wants it) still acts; the default
    manual endpoint uses debounce=True so re-clicks don't reshuffle. Verify the
    debounce flag is what gates the second rebuild."""
    w1, w2, w3, w4 = _past_starts()
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300)])
    pd1, a1, _i, _s = _apply(plan, ctl=30, tsb=10, activities=[])
    assert a1 == "rebuilt"
    # debounce OFF → acts again (force); debounce ON → latched.
    _pd2, a_force, _i2, _s2 = _apply(pd1, ctl=30, tsb=10, activities=[], debounce=False)
    _pd3, a_latched, _i3, _s3 = _apply(pd1, ctl=30, tsb=10, activities=[], debounce=True)
    assert a_force == "rebuilt"
    assert a_latched == "rebalanced"


# ── recent-gap gate (stickiness fix) ─────────────────────────────────────────

def test_old_recovered_gap_does_not_trigger_rebuild():
    """Weeks 1-2 missed but the most-recent completed week (3) trained normally
    → consecutive_missed≥2 yet NO rebuild (gap is historical)."""
    w1, w2, w3, w4 = _past_starts()
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300),
                       _mkweek(5, T + timedelta(days=7), 300)])
    acts = _full_adherence_activities([w3])  # only the recent week trained
    _pd, action, info, _s = _apply(plan, ctl=48, tsb=-3, activities=acts)
    assert info["gaps"]["consecutive_missed"] >= 2  # the old gap is detected …
    assert action == "rebalanced"                   # … but does not rebuild


# ── event-taper guard ────────────────────────────────────────────────────────

def test_latch_band_reflects_current_episode_not_old_longer_gap():
    """Guard 5: an OLDER, longer recovered gap must not poison the latch key.
    Weeks 1-3 missed (old, 3wk), weeks 4-5 trained, weeks 6-7 missed (current,
    2wk). The latch key's band must come from the CURRENT 2-week episode
    (anchored at week 6), not detect_plan_gaps' global 3-week max."""
    # week 1 = T-49 … week 7 = T-7 (last completed), week 8 = T (current week).
    base = T - timedelta(days=49)
    weeks = [_mkweek(n, base + timedelta(days=7 * (n - 1)), 300) for n in range(1, 9)]
    plan = _base_plan(weeks)
    # only weeks 4 (T-28) & 5 (T-21) trained → old gap weeks 1-3, current gap weeks 6-7
    acts = _full_adherence_activities([base + timedelta(days=7 * 3),
                                       base + timedelta(days=7 * 4)])
    pd, action, info, _s = _apply(plan, ctl=35, tsb=8, activities=acts)
    assert action == "rebuilt"
    key = pd.get("gap_regen_latch", {}).get("key", "")
    assert key.startswith("6:"), f"latch anchored at wrong week: {key}"
    assert key.endswith(":s"), f"band should be 's' (2-week episode), got: {key}"


def test_event_taper_window_flags_behind_not_silent_rebuild():
    w1, w2, w3, w4 = _past_starts()
    goal = {"type": "event", "event_date": (T + timedelta(days=10)).isoformat(),
            "hours_per_week": 8, "max_weekday_hours": 2.0,
            "max_weekend_hours": 3.5, "rest_days": [2, 6], "longest_ride_h_90d": 2.0}
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300)], goal=goal)
    pd, action, _info, status = _apply(plan, ctl=30, tsb=10, activities=[])
    assert action == "rebalanced"  # did NOT silently rebuild the taper
    assert pd.get("last_update_info", {}).get("behind_plan") is True
    assert "event" in status.lower()


def test_event_outside_taper_window_rebuilds():
    """Same missed gap but the event is far out (>21d) → normal rebuild."""
    w1, w2, w3, w4 = _past_starts()
    goal = {"type": "event", "event_date": (T + timedelta(days=90)).isoformat(),
            "hours_per_week": 8, "max_weekday_hours": 2.0,
            "max_weekend_hours": 3.5, "rest_days": [2, 6], "longest_ride_h_90d": 2.0}
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300),
                       _mkweek(5, T + timedelta(days=7), 300)], goal=goal)
    pd, action, _info, _s = _apply(plan, ctl=30, tsb=10, activities=[])
    assert action == "rebuilt"
    # event date is immovable
    assert pd["goal"]["event_date"] == goal["event_date"]


# ── invariants on rebuild ────────────────────────────────────────────────────

def test_rebuild_preserves_top_level_keys_and_past_weeks():
    w1, w2, w3, w4 = _past_starts()
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300)])
    plan["availability"] = {w1.isoformat(): {"hours": 1.5, "type": "available"}}
    plan["some_user_key"] = "keepme"
    pd, action, _info, _s = _apply(plan, ctl=30, tsb=10, activities=[])
    assert action == "rebuilt"
    # top-level keys survive (v1.8.20 shallow-copy contract)
    assert pd.get("some_user_key") == "keepme"
    assert "availability" in pd
    # past weeks kept verbatim (start dates still present in the rebuilt plan)
    starts = {w["start"] for w in pd["weeks"]}
    assert w1.isoformat() in starts and w2.isoformat() in starts


# ── v3.7.1: VO2max microintervals-only preference ────────────────────────────

def _zwo(*segments):
    """A .zwo tree from (tag, attrs) pairs, for testing the measure itself."""
    import xml.etree.ElementTree as ET
    root = ET.Element("workout")
    for tag, attrs in segments:
        ET.SubElement(root, tag, {k: str(v) for k, v in attrs.items()})
    return root


def test_what_counts_as_a_microinterval_session():
    """Ronnestad's protocol is series of 5-13 x (30 s ON / 15 s OFF) at ~118 %
    of 40-min maximal power, ~60 % on the OFF leg (PMC11680548); the wider
    short-interval literature runs 10-40 s bouts at ~100-130 % of maximal
    aerobic power with recovery at or under the work, and contrasts them with
    4-5 min intervals. The short recovery is the mechanism: oxygen uptake does
    not fall between reps.

    Each case below turns one of those numbers, so a rule that stops being
    enforced fails here."""
    def shape(*segs):
        return tp._micro_shape(_zwo(*segs))

    def is_micro(*segs):                    # the verdict the planner reads
        return tp.is_microinterval(*shape(*segs))

    ss = lambda d, p: ("SteadyState", {"Duration": d, "Power": p})
    ivl = lambda n, on, off, p, offp=0.6: ("IntervalsT", {
        "Repeat": n, "OnDuration": on, "OffDuration": off, "OnPower": p, "OffPower": offp})
    reps, micro_s, long_s = shape(ivl(13, 30, 15, 1.18))
    assert (reps, micro_s, long_s) == (13, 390, 0), "the protocol itself"

    # Each rule is pinned AT ITS EDGE, both sides. A review found four
    # constants that survived every mutation because the only case near them
    # sat several steps away: the recovery ratio passed at 1.0, 2.0 and 4.9.
    # the bout ceiling: 60 s reps are the format it is contrasted against
    assert shape(ivl(10, 60, 30, 1.10))[1] == 0, "a 60 s rep is not a microinterval"
    assert shape(ivl(10, 40, 20, 1.15))[1] == 400, "40/20 is in the family"
    assert shape(ivl(10, 41, 20, 1.15))[1] == 0, "one second over the ceiling is out"

    # the recovery ratio: full recovery is anaerobic work, not this protocol
    assert shape(ivl(10, 30, 150, 1.25))[1] == 0, "30 s with 150 s recovery"
    assert shape(ivl(10, 30, 30, 1.10))[1] == 300, "30/30 still qualifies"
    assert shape(ivl(10, 30, 45, 1.10))[1] == 300, "1.5x the bout is the edge"
    assert shape(ivl(10, 30, 46, 1.10))[1] == 0, "and one second past it is out"

    # the power band: below FTP is not work, above 1.5 reads neuromuscular
    assert shape(ivl(10, 30, 15, 0.80))[1] == 0
    assert shape(ivl(10, 30, 15, 0.99))[1] == 0, "under FTP is not the work leg"
    assert shape(ivl(10, 30, 15, 1.00))[1] == 300, "FTP itself is in"
    assert shape(ivl(10, 20, 20, 2.00))[1] == 0
    assert shape(ivl(10, 20, 20, 1.51))[1] == 0, "past 1.50 reads neuromuscular"
    assert shape(ivl(10, 20, 20, 1.50))[1] == 200, "1.50 is the top of the band"

    # the recovery-power ceiling, both sides
    assert shape(ivl(10, 30, 30, 1.15, 0.80))[1] == 300, "0.80 still recovers"
    assert shape(ivl(10, 30, 30, 1.15, 0.81))[1] == 0, "past it, a second effort"


    # the recovery must recover. An over-under alternating 30 s at 1.15 with
    # 30 s at 0.95 never lets oxygen uptake fall, which is the whole mechanism,
    # so it is not this protocol however short its legs are.
    assert shape(ivl(10, 30, 30, 1.15, 0.60))[1] == 300, "an easy recovery leg"
    assert shape(ivl(10, 30, 30, 1.15, 0.95))[1] == 0, "a 0.95 leg is a second effort"
    assert shape(*[x for _ in range(10) for x in (ss(30, 1.15), ss(30, 0.95))])[1] == 0

    # a main set written as SteadyState or Ramp blocks is still a main set
    assert shape(*[x for _ in range(6) for x in (ss(30, 1.2), ss(15, 0.6))])[0] == 6
    assert shape(("Ramp", {"Duration": 240, "PowerLow": 0.85, "PowerHigh": 1.2}))[2] == 240

    # A SERIES is consecutive reps. Six sets of four, written as pairs with a
    # three-minute rest between sets, are six sets of four -- reading them as
    # one series of 24 made the same session qualify or not depending only on
    # whether its author used IntervalsT or SteadyState pairs.
    _set4 = [x for _ in range(4) for x in (ss(30, 1.2), ss(15, 0.6))]
    assert shape(*[x for _ in range(6) for x in (_set4 + [ss(180, 0.5)])])[0] == 4
    assert shape(*(_set4 + [("FreeRide", {"Duration": 300})] + _set4))[0] == 4
    # ...and the last rep of a series counts, though the series rest follows it
    assert shape(*([x for _ in range(4) for x in (ss(30, 1.2), ss(15, 0.6))]
                   + [ss(30, 1.2), ss(300, 0.5)]))[0] == 5

    # Sustained work is INTEGRATED, not averaged. A 15-min ramp to 1.0 spends
    # its last 270 s at or over the line; scored at its 0.75 mean it vanished,
    # and a sweet-spot pyramid carrying eight 40/20s read as microintervals.
    _pyramid = (("Ramp", {"Duration": 900, "PowerLow": 0.5, "PowerHigh": 1.0}),
                ("Ramp", {"Duration": 900, "PowerLow": 1.0, "PowerHigh": 0.5}))
    assert shape(*_pyramid)[2] == 540, "270 s above the line in each direction"
    assert not is_micro(*(_pyramid + (ivl(8, 40, 20, 1.15),)))
    # a warm-up ramp that never reaches the line stays invisible
    assert shape(("Warmup", {"Duration": 600, "PowerLow": 0.4, "PowerHigh": 0.75}))[2] == 0

    assert is_micro(ivl(13, 30, 15, 1.18))
    assert not is_micro(ivl(6, 30, 15, 1.18)), "under 5 min of work"
    assert not is_micro(ivl(13, 30, 15, 1.18), ivl(6, 300, 180, 1.05)), "a longer effort dominates"
    # a sweet-spot main set is sustained work even though it is under FTP
    assert not is_micro(ivl(13, 30, 15, 1.18), ivl(4, 360, 180, 0.90)), "4x6min at 0.90 dominates"
    assert is_micro(ivl(13, 30, 15, 1.18), ivl(4, 360, 180, 0.70)), "an endurance block does not"
    # and the line itself, both sides
    assert not is_micro(ivl(13, 30, 15, 1.18), ivl(4, 360, 180, 0.85)), "0.85 competes"
    assert is_micro(ivl(13, 30, 15, 1.18), ivl(4, 360, 180, 0.84)), "0.84 does not"
    # 12 min of short work, but in fours: openers repeated, not a series
    assert not is_micro(*[ivl(4, 30, 15, 1.18)] * 6), "under a series: an opener, not a session"


def test_the_library_measure_agrees_with_the_definition():
    """The index caches the verdict, so this recomputes it from the .zwo files
    themselves: a stale or drifting index fails here rather than quietly
    serving the wrong workouts."""
    import xml.etree.ElementTree as ET
    lib = tp.load_workout_library()
    by = {w["File"]: w for w in lib}

    def verdict(name):
        path = pathlib.Path(tp.WORKOUT_DIR) / name
        root = ET.parse(path).getroot()
        el = root.find("workout") if root.find("workout") is not None else root
        return tp.is_microinterval(*tp._micro_shape(el))

    expect_micro = ("vo2_short_3x13x30s-15s_113pct_55min.zwo",
                    "vo2_short_3x8x40s-20s_115pct_46min.zwo")
    expect_not = ("vo2_short_2x5x2min_115pct_81min.zwo",       # 10x2min
                  "vo2max_3x2x1min_100pct_63min.zwo",          # 1-min reps
                  "vo2max_6x4min-2min_85pct_63min.zwo",        # main set as Ramps
                  "vo2max_3x3min-3min_110pct_61min.zwo",       # main set as IntervalsT
                  "anaerobic_16x30s-150s_125pct_78min.zwo",    # 30 s, full recovery
                  "vo2_short_15x30s-30s_125pct_60min.zwo")     # 15x30/30 beside an 8-min effort
    for f in expect_micro + expect_not:
        want = f in expect_micro
        assert verdict(f) is want, f"{f}: the rule says {verdict(f)}, expected {want}"
        assert bool(by[f]["IsMicro"]) is want, f"{f}: the index disagrees with the rule"


def test_the_index_is_rebuilt_when_a_rule_changes(tmp_path, monkeypatch):
    """The index caches verdicts, not just parsed files, so editing a threshold
    must invalidate it. Nothing about the library changes when a constant is
    edited -- same files, same count, same mtimes -- so without the rules in
    the header the old conclusions are served forever, which is exactly how a
    measured 150 files kept reading as 160."""
    src = pathlib.Path(tp.WORKOUT_DIR) / "vo2_short_3x13x30s-15s_113pct_55min.zwo"
    lib_dir = tmp_path / "workouts"
    lib_dir.mkdir()
    (lib_dir / src.name).write_bytes(src.read_bytes())
    monkeypatch.setattr(tp, "WORKOUT_DIR", lib_dir)
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()

    assert tp.load_workout_library()[0]["IsMicro"] is True
    written = json.loads((lib_dir / tp._LIBRARY_INDEX_FILENAME).read_text())
    assert written.get("rules") == tp._index_rules_fingerprint(), (
        "the header must carry the rules it was built under, or every cold "
        "load rejects the index and re-parses 4,307 files")

    # Same files, same mtimes: only the rule moved. Patched in zwo_parse,
    # which is where micro_shape and rules_fingerprint read it -- the name on
    # training_planner is a re-export, and patching that would change nothing
    # (which is the whole reason the next test pins the two as one object).
    monkeypatch.setattr(zwo_parse, "MICRO_MIN_SERIES_REPS", 99)
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()
    assert tp.load_workout_library()[0]["IsMicro"] is False, "served a stale verdict"


def test_microintervals_only_serves_microintervals_and_still_fills_the_slot():
    """ON must serve a microinterval session wherever the library has one that
    fits -- the scoring penalty alone left about one slot in nine to a
    long-rep file, because the weighted draw floors every weight at 0.1 -- and
    must never leave a slot unfilled."""
    lib = tp.load_workout_library()
    by = {w["File"]: w for w in lib}

    def sweep(flag):
        hit = tot = 0
        for wk in range(1, 61):
            for day in (1, 3):
                for dur in (45, 60, 75):
                    s = tp.PlannedSession(day="", day_name="", session_type="vo2max",
                                          duration_min=dur, tss_estimate=int(dur * 1.3),
                                          description="")
                    f = tp.match_zwo(s, lib, week_num=wk, day_idx=day, micro_only=flag).zwo_file
                    assert f, "microintervals-only left a VO2max slot unfilled"
                    tot += 1
                    hit += bool((by.get(f) or {}).get("IsMicro"))
        return hit, tot

    off_hit, tot = sweep(False)
    on_hit, _ = sweep(True)
    assert on_hit == tot, (on_hit, tot)
    assert off_hit < tot, "OFF must keep the variety the library is for"


def test_the_index_notices_a_changed_MEASURE_not_just_a_changed_threshold(
        tmp_path, monkeypatch):
    """Editing the rule's CODE has to invalidate the index too.

    This is the hole the previous version left, and it was not theoretical:
    the index committed on 2026-09-16 was stale by 517 fields, because two
    fixes to micro_shape (counting a series' last rep, integrating a ramp
    instead of averaging it) changed what the rows say without touching a
    single threshold. Count, mtime and every constant still matched, so the
    stale conclusions were served.
    """
    before = zwo_parse.rules_fingerprint()

    def micro_shape_but_different(workout_el):      # same rules, other code
        reps, micro_s, long_s = zwo_parse.micro_shape(workout_el)
        return reps, micro_s, long_s

    monkeypatch.setattr(zwo_parse, "micro_shape", micro_shape_but_different)
    assert zwo_parse.rules_fingerprint() != before, (
        "the fingerprint must cover the measure's code, not only its numbers")


def test_the_planner_re_exports_the_one_measure():
    """`training_planner.X` and `zwo_parse.X` must BE the same objects.

    The measure moved to a leaf module; the planner keeps the old names
    because the library index, the matcher and 200+ test files reach them
    there. A re-export that silently rebound to a copy would give the two a
    way to disagree, which is the exact failure the extraction was meant to
    remove.
    """
    for name in ("MICRO_MAX_BOUT_S", "MICRO_MAX_REST_RATIO", "MICRO_MIN_POWER",
                 "MICRO_MAX_POWER", "MICRO_MAX_REST_POWER", "MICRO_LONG_MIN_POWER",
                 "MICRO_MIN_SERIES_REPS", "MICRO_MIN_WORK_S"):
        assert getattr(tp, name) == getattr(zwo_parse, name), name
    assert tp._micro_shape is zwo_parse.micro_shape
    assert tp.is_microinterval is zwo_parse.is_microinterval
    assert tp._index_rules_fingerprint is zwo_parse.rules_fingerprint
    assert tp._np_fraction_from_samples is zwo_parse.np_fraction_from_samples
    # app held a third, byte-identical copy of the NP fraction, kept in step
    # with the planner's by a comment. All three names are one object now.
    import app
    assert app._np_fraction_from_samples is zwo_parse.np_fraction_from_samples
    assert app._scan_zwo_for_library is zwo_parse.scan_zwo


def test_microintervals_only_holds_through_a_whole_generated_plan(freeze_clock):
    """The preference has to survive the PLAN, not just match_zwo.

    Six places build a session straight from a library row and two more
    rematch one; the flag lived in match_zwo alone, so a review found about
    half the VO2max days of a real plan coming back long-rep while the
    match_zwo-level test above read 60/60. This one asks generate_plan.
    """
    import datetime as _dt
    freeze_clock(_dt.datetime(2026, 9, 14, 9, 0))
    lib = tp.load_workout_library()
    by = {w["File"]: w for w in lib}

    def plan(micro_only, salt):
        goal = tp.Goal(goal_type="continuous", rest_days=[6],
                       available_days=[0, 1, 2, 3, 4, 5],
                       daily_max_hours={d: 2.0 for d in range(6)})
        goal.vo2_microintervals_only = micro_only
        _phases, weeks = tp.generate_plan(goal, seed_salt=salt, current_ctl=60.0,
                                          recent_weekly_tss=400.0)
        served, unfilled = [], 0
        for wk in weeks:
            for sess in wk.sessions:
                if sess.session_type != "vo2max":
                    continue
                if not (sess.zwo_file or ""):
                    unfilled += 1
                    continue
                served.append(sess.zwo_file)
        return served, unfilled

    on_total = on_micro = off_total = off_micro = 0
    for salt in (0, 3, 7, 11, 42, 99, 4242):
        served, unfilled = plan(True, salt)
        assert unfilled == 0, f"salt {salt}: microintervals-only left a VO2max day empty"
        for f in served:
            assert by.get(f, {}).get("IsMicro"), (
                f"salt {salt}: {f} is not a microinterval session, and the "
                f"library has one that fits")
        on_total += len(served)
        on_micro += len(served)
        served_off, _ = plan(False, salt)
        off_total += len(served_off)
        off_micro += sum(1 for f in served_off if by.get(f, {}).get("IsMicro"))

    assert on_total >= 7, f"the flag must not empty the plan of VO2max work ({on_total})"
    assert off_micro < off_total, "OFF must keep the variety the library is for"


def test_the_preference_defaults_off_and_never_goes_stale():
    """The preference travels with the call, never with the process: one
    microinterval-only match cannot leak into the next (notes/review/state.md
    STA-5 -- a single swap used to keep filtering later rematches)."""
    assert tp.Goal.__dataclass_fields__["vo2_microintervals_only"].default is False
    lib = tp.load_workout_library()

    def slot():
        return tp.PlannedSession(day="", day_name="", session_type="vo2max",
                                 duration_min=60, tss_estimate=80, description="")
    before = tp.match_zwo(slot(), lib, week_num=3, day_idx=2).zwo_file
    tp.match_zwo(slot(), lib, week_num=3, day_idx=2, micro_only=True)
    assert tp.match_zwo(slot(), lib, week_num=3, day_idx=2).zwo_file == before
