"""One athlete, one plan, one week of rides — the fixture every endpoint is
asked about in tools/arch/quantities.py.

It is deliberately ordinary: a rider mid-week with three rides behind them,
a plan that covers the calendar week, wellness rows intervals.icu could have
sent, and an FTP. Nothing here is edge-case; the point is that the SAME
ordinary state is visible through every door, so any disagreement between two
endpoints is about the doors, not the state.
"""
from __future__ import annotations

import json
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

TODAY = date(2026, 9, 17)                       # a Thursday
MONDAY = TODAY - timedelta(days=TODAY.weekday())
FTP = 250
CTL, ATL = 42.0, 51.0                           # TSB = -9.0
PLANNED = {0: 101, 1: 55, 2: 51, 3: 43, 4: 137, 5: 0, 6: 60}
WEEK_BUDGET = 500.0            # what the ramp allows; the sessions carry 447
# What the rider actually did, Monday to Wednesday.
RIDES = [
    {"day": 0, "tss": 96.0, "duration_s": 5400, "moving_s": 5100, "watts": 178},
    {"day": 1, "tss": 52.0, "duration_s": 3900, "moving_s": 3600, "watts": 150},
    {"day": 2, "tss": 61.0, "duration_s": 4500, "moving_s": 4200, "watts": 155},
]


def _ride_record(i: int, r: dict) -> dict:
    day = MONDAY + timedelta(days=r["day"])
    return {
        "ride_id": f"icu_i{9000 + i}", "external_id": f"i{9000 + i}",
        "source": "icu", "name": f"Ride {i}",
        "started_at": f"{day.isoformat()}T09:00:00",
        "date": day.isoformat(),
        "duration_s": r["duration_s"], "moving_s": r["moving_s"],
        "tss": r["tss"], "avg_power_w": r["watts"],
        "distance_km": 30.0 + i, "elevation_m": 200,
        "time_in_zone": {"z1": 900, "z2": 2100, "z3": 600, "z4": 300,
                         "z5": 120, "z6": 60, "z7": 20},
    }


def _plan() -> dict:
    weeks = []
    for wk in range(-1, 3):
        start = MONDAY + timedelta(weeks=wk)
        sessions = []
        for d in range(7):
            day = start + timedelta(days=d)
            tss = PLANNED[d]
            sessions.append({
                "day": day.isoformat(), "day_name": day.strftime("%a"),
                "session_type": "rest" if tss == 0 else "z2",
                "duration_min": 0 if tss == 0 else tss + 20,
                "tss_estimate": tss, "description": "steady",
                "status": "done" if day < TODAY else "pending",
                "zwo_file": "" if tss == 0 else "z2_steady_56pct_60min.zwo",
                "zwo_name": "" if tss == 0 else "Z2",
            })
        # The stored budget is NOT the sum of the sessions. A ramp allows 500;
        # the days carry 447. Both numbers are legitimate and they are
        # different questions, which is invisible while a fixture makes them
        # equal -- and they travel under the same key name today.
        weeks.append({"week_num": wk + 2, "start": start.isoformat(),
                      "end": (start + timedelta(days=6)).isoformat(),
                      "phase": "base", "tss_target": WEEK_BUDGET,
                      "is_stepback": False, "sessions": sessions})
    return {
        "goal": {"type": "general", "event_date": (TODAY + timedelta(weeks=10)).isoformat()},
        "phases": [], "weeks": weeks,
        "generated": (TODAY - timedelta(days=14)).isoformat(),
        "ctl_snapshot": {"current_ctl": CTL, "recent_weekly_tss": 350.0,
                         "generated_on": (TODAY - timedelta(days=14)).isoformat()},
    }


SCENARIOS = ("normal", "icu_down", "replanned_midweek")


@contextmanager
def build(shift_one_endpoint: bool = False, scenario: str = "normal"):
    """Yield ``(client, fixtures)``: a TestClient over the fixture, and the
    path-parameter values (ride ids, filenames) the probe needs to address
    parameterised routes.

    ``shift_one_endpoint`` is the negative control: one reader's fitness is
    moved by a known amount so a probe that cannot see a disagreement fails
    loudly instead of reporting a clean bill of health.

    ``scenario`` picks the state to ask about. The healthy one is the least
    interesting: every disagreement this project has shipped appeared in a
    DEGRADED state, where one reader falls back and another does not.

      normal              intervals.icu answers, plan covers the week
      icu_down            the network is gone; readers fall back on their own
      replanned_midweek   the plan was rebuilt on Thursday, after three rides

    The fixture deliberately exercises the rungs that a comfortable fixture
    skips, because each one hid a real disagreement:

      * intervals.icu's own activity list is in the training cache, so the
        endpoints that prefer it (the home page's) are compared with the
        endpoints that read the stored envelopes;
      * the week's stored budget is NOT the sum of its sessions, so the three
        quantities that have been sharing the name `tss_target` come apart;
      * the profile's wellness archive exists AND differs from the SQLite
        wellness table, because they have different writers;
      * HRV and sleep rows are present, so the readiness composite is awake
        rather than dark.
    """
    import cache
    import clock
    import db
    import ride_storage
    import app as app_module
    from fastapi.testclient import TestClient

    clock.freeze(TODAY)
    # `cache._cache` is a process-global with a 300 s TTL. A `training` entry
    # left by any earlier test in the same worker is served instead of this
    # fixture's ICU stub, and the probe then compares the app against numbers
    # nobody in this file chose: in one parallel run the live readers said CTL
    # 50 while the fixture's own store said 42, and the probe reported the app
    # disagreeing with itself. A probe that does not control its inputs
    # measures the suite, not the app.
    cache.clear_cache()
    tmp = Path(tempfile.mkdtemp(prefix="quantities_"))
    plan_dir, rides_dir = tmp / "plan", tmp / "rides" / "icu"
    plan_dir.mkdir(parents=True), rides_dir.mkdir(parents=True)
    (plan_dir / "current_plan.json").write_text(json.dumps(_plan()), encoding="utf-8")

    records = [_ride_record(i, r) for i, r in enumerate(RIDES)]
    for rec in records:
        (rides_dir / f"{rec['external_id']}.json").write_text(
            json.dumps(rec), encoding="utf-8")

    # The fixture owns its database, so the probe is the same run to run and
    # standalone runs (no conftest sandbox) work. set_db_path is paired with
    # close_all_connections because on this branch the version bump is still
    # the caller's job -- the very footgun the db patch on upstream#14 removes.
    original_db = db.DB_PATH
    db.set_db_path(tmp / "health_tracker.db")
    db.close_all_connections()
    db.init_db()

    conn = db.get_db()
    conn.execute("DELETE FROM wellness")
    for i in range(45):
        d = (TODAY - timedelta(days=i)).isoformat()
        conn.execute("INSERT OR REPLACE INTO wellness (date, ctl, atl) VALUES (?,?,?)",
                     (d, CTL, ATL))
    conn.commit()

    # The profile's own wellness archive, written by the lazy sync on every
    # home-page load, while the SQLite table above is written by the 30-minute
    # loop. TWO stores with TWO writers for one fact. They hold the same
    # numbers here: the probe's job is to prove the readers agree, not to
    # prove they disagree, so the stores are seeded consistently and the
    # ownership test is what forbids the second store existing at all.
    wellness_dir = tmp / "wellness"
    wellness_dir.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        d = (TODAY - timedelta(days=i)).isoformat()
        (wellness_dir / f"{d}.json").write_text(json.dumps({
            "id": d, "ctl": CTL, "atl": ATL, "restingHR": 45, "hrv": 62.0,
            "sleepSecs": 27000, "sleepScore": 82.0,
            "sportInfo": [{"type": "Ride", "eftp": FTP}],
        }), encoding="utf-8")

    # intervals.icu's own activity list, the rung the home page prefers. ICU
    # reports MOVING time; the stored envelopes carry both moving and elapsed.
    # Without this the most-read endpoint in the app never took its first rung
    # and the 118-versus-91 class of bug could not be seen.
    icu_activities = [{
        "date": r["date"], "name": r["name"], "sport": "Ride",
        "duration_min": round(r["moving_s"] / 60), "tss": r["tss"],
        "avg_power": r["avg_power_w"], "avg_hr": 140, "id": r["external_id"],
    } for r in records]

    icu_metrics = {"ctl": CTL, "atl": ATL, "ftp": FTP,
                   "recent_activities": icu_activities}

    def _today_metrics(*a, **kw):
        if scenario == "icu_down":
            # What the app sees when the network is gone: training.py logs and
            # hands back nothing, and every reader decides for itself what to
            # show instead. That decision is where the CTL owners drifted
            # apart before, so it is the scenario worth asking about.
            raise __import__("training").ICUNetworkError("network unreachable")
        return dict(icu_metrics)

    patches = [
        patch.object(app_module, "_plan_dir", return_value=plan_dir),
        # EVERY profile-scoped archive, not just the rides. Redirecting
        # _icu_rides_dir alone left ride_storage._wellness_dir() pointing at
        # the real ~/.domestique, so the probe read the developer's own
        # wellness history and reported a disagreement that was the live data
        # meeting the fixture. One seam, so a new archive dir cannot leak.
        patch.object(ride_storage, "_active_profile_dir", return_value=tmp),
        patch.object(ride_storage, "_icu_rides_dir", return_value=rides_dir),
        patch.object(app_module, "_kick_lazy_icu_sync", return_value=False),
        patch("training.get_today_metrics", _today_metrics),
        patch.object(app_module, "_settings_ftp", return_value=FTP)
        if hasattr(app_module, "_settings_ftp") else None,
    ]
    if shift_one_endpoint:
        # The control: ONE door answers differently. /api/readiness reports a
        # CTL 7 higher; every other endpoint keeps the fixture's. It has to be
        # one endpoint rather than the shared helper: shifting `_fitness_state`
        # moves every live reader together, and the probe then detects only
        # "live readers vs the plan's stored snapshot" -- which it would still
        # do after losing the ability to compare two live readers at all.
        # Patched on the ROUTE, not the module attribute: FastAPI captured the
        # function object at registration, so rebinding app.api_readiness
        # changes nothing the client can see -- the first version of this
        # control did that and reported a clean run while holding a shifted
        # endpoint in its hand.
        route = next(r for r in app_module.app.routes
                     if getattr(r, "path", "") == "/api/readiness")
        real_call = route.dependant.call

        def shifted_readiness(*a, **kw):
            out = real_call(*a, **kw)
            try:
                t = out["training"]
                if t.get("ctl") is not None:
                    t["ctl"] = t["ctl"] + 7.0
            except (TypeError, KeyError):
                pass
            return out
        patches.append(patch.object(route.dependant, "call", shifted_readiness))

    started = [p for p in patches if p is not None]
    for p in started:
        p.start()
    if scenario == "replanned_midweek":
        # The rider rebuilt the plan on Thursday, three rides into the week.
        # This is the state that read 499 TSS done against 138 planned while
        # intervals.icu read 499/387, so it is the state the doors have to
        # agree about.
        with patch.object(app_module, "cached",
                          side_effect=lambda k, fn, **kw: fn() if k != "training"
                          else dict(icu_metrics)), \
             patch.object(app_module.db, "query_activities",
                          return_value=[{"date": r["date"], "tss": r["tss"],
                                         "duration_min": r["duration_s"] // 60,
                                         "sport": "Ride", "avg_hr": 140,
                                         "name": r["name"]} for r in records]):
            resp = TestClient(app_module.app).post("/api/plan/regenerate")
            if resp.status_code != 200:            # a fixture that did not set
                raise AssertionError(               # up is not a passing probe
                    f"replan fixture failed: HTTP {resp.status_code} {resp.text[:200]}")
    try:
        yield TestClient(app_module.app), {
            "ride_id": records[0]["ride_id"],
            "activity_id": records[0]["external_id"],
            "external_id": records[0]["external_id"],
            "filename": "z2_steady_56pct_60min.zwo",
            "day": TODAY.isoformat(),
            "date": TODAY.isoformat(),
        }
    finally:
        for p in reversed(started):
            p.stop()
        db.set_db_path(original_db)
        db.close_all_connections()
        cache.clear_cache()          # leave nothing behind either
        clock.unfreeze()
