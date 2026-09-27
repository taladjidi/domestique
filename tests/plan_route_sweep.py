"""G3 (contract 10.6): every write under a plan dir, with its stack, while
every route, the cron sequence, the background hooks, the boot passes and a
profile switch run on a sandbox home. Run as a subprocess by
tests/test_one_plan_route_sweep.py (it owns its HOME, its clock and its
event loop); ported from the single-source audit's rt_audit.py.

A write is OWNED when the innermost src/ frame on its stack is plan_store.py
(the store's own writer). Before plan_store existed the door was
training_planner.update_plan: such writes are classed "legacy", the rest
"none" -- both are the worklist.

The mutation audit wraps the door: a mutator that returns a plan read
OUTSIDE the store's read-modify-write (or carries its weeks/goal) wrote a
stale copy through the door.

usage: plan_route_sweep.py OUT.json [--plant]
"""
from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parent / "src"
OUT = Path(sys.argv[1])
PLANT = "--plant" in sys.argv
H = Path(tempfile.mkdtemp(prefix="dq-sweep-"))
os.environ.update(HOME=str(H), DOMESTIQUE_HOME=str(H / ".domestique"),
                  DOMESTIQUE_ALLOWED_HOSTS="testserver", DOMESTIQUE_NO_NET="1")
sys.path.insert(0, str(SRC))
sys.path.insert(1, str(TESTS))
DH = H / ".domestique"

REC: list[dict] = []
_tl = threading.local()
STATE = {"route": "import", "probe": False}


def _is_plan_path(p) -> bool:
    try:
        ap = Path(os.path.abspath(os.fspath(p)))
    except TypeError:
        return False
    try:
        rel = ap.relative_to(DH)
    except ValueError:
        return False
    return "plans" in rel.parts or ap.name.startswith("current_plan")


def _contains_plans(p) -> bool:
    try:
        ap = Path(os.path.abspath(os.fspath(p)))
    except TypeError:
        return False
    if not ap.is_dir():
        return False
    if ap != DH and DH not in ap.parents:
        return False
    return any(True for _ in ap.rglob("current_plan.json"))


def _door(start=3):
    """(stack, door) for the current write."""
    out, door, legacy = [], None, None
    f = sys._getframe(start)
    while f is not None:
        fn = f.f_code.co_filename
        if door is None and fn.startswith(str(SRC)):
            door = "store" if fn.endswith("plan_store.py") else "none"
        if legacy is None and f.f_code.co_name == "atomic_write_plan" and fn.endswith("training_planner.py"):
            b = f.f_back
            legacy = bool(b and b.f_code.co_name == "update_plan"
                          and b.f_code.co_filename.endswith("training_planner.py"))
        if fn.startswith(str(SRC)) or fn == __file__:
            out.append(f"{Path(fn).name}:{f.f_lineno}:{f.f_code.co_name}")
        f = f.f_back
    if door != "store" and legacy:
        door = "legacy"
    return out, door or "none"


def _record(op, *paths, ancestor=False):
    if STATE["probe"] or getattr(_tl, "busy", False):
        return
    hit = [p for p in paths if p is not None and (_is_plan_path(p) or (ancestor and _contains_plans(p)))]
    if not hit:
        return
    _tl.busy = True
    try:
        st, door = _door()
        REC.append({"route": STATE["route"], "thread": threading.current_thread().name,
                    "op": op, "path": [str(Path(os.fspath(p))).replace(str(DH), "~D") for p in hit],
                    "door": door, "stack": st[:14]})
    finally:
        _tl.busy = False


def _wrap(mod, name, op, argidx=(0,), mode_check=None, ancestor=False):
    orig = getattr(mod, name)

    def w(*a, **k):
        if mode_check is None or mode_check(a, k):
            _record(op, *[a[i] if i < len(a) else None for i in argidx], ancestor=ancestor)
        if getattr(_tl, "busy", False):
            return orig(*a, **k)
        _tl.busy = ancestor
        try:
            return orig(*a, **k)
        finally:
            _tl.busy = False
    w.__wrapped__ = orig
    setattr(mod, name, w)


def _open_writes(a, k):
    mode = a[1] if len(a) > 1 else k.get("mode", "r")
    return isinstance(mode, str) and any(c in mode for c in "wax+")


def _osopen_writes(a, k):
    flags = a[1] if len(a) > 1 else k.get("flags", 0)
    return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))


_wrap(builtins, "open", "open", (0,), _open_writes)
io.open = builtins.open
_wrap(os, "open", "os.open", (0,), _osopen_writes)
for n in ("replace", "rename"):
    _wrap(os, n, f"os.{n}", (0, 1))
for n in ("remove", "unlink", "rmdir", "truncate"):
    _wrap(os, n, f"os.{n}", (0,))
for n in ("copy", "copy2", "copyfile"):
    _wrap(shutil, n, f"shutil.{n}", (1,))
_wrap(shutil, "move", "shutil.move", (0, 1), ancestor=True)
_wrap(shutil, "copytree", "shutil.copytree", (1,), ancestor=True)
_wrap(shutil, "rmtree", "shutil.rmtree", (0,), ancestor=True)

import clock  # noqa: E402

TODAY = date(2026, 9, 23)          # a Wednesday
clock.freeze(TODAY)

import app as A  # noqa: E402
import training_planner as tp  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from profile_manager import ProfileManager  # noqa: E402

try:
    import plan_store as PS  # noqa: E402
except ImportError:
    PS = None
if PS is not None and hasattr(PS, "allow_test_writes"):
    PS.allow_test_writes(TESTS)     # the sweep seeds plans itself (probe_write)

# ── the mutation audit ───────────────────────────────────────────────────────
OUTSIDE: dict = {}      # id(plan) -> (plan, route): plans read outside the door
MUT: list[dict] = []

if PS is not None:
    _orig_read = PS.read

    def _audited_read(*a, **k):
        r = _orig_read(*a, **k)
        if isinstance(r, dict):
            OUTSIDE[id(r)] = (r, STATE["route"])
        return r
    PS.read = _audited_read
else:
    _json_load = json.load

    def _jl(fp, *a, **k):
        r = _json_load(fp, *a, **k)
        name = getattr(fp, "name", "")
        if isinstance(name, str) and name.endswith("current_plan.json") and isinstance(r, dict):
            f, inside = sys._getframe(1), False
            while f is not None:
                if f.f_code.co_name == "update_plan":
                    inside = True
                    break
                f = f.f_back
            if not inside:
                OUTSIDE[id(r)] = (r, STATE["route"])
        return r
    json.load = _jl


def _stale(new) -> "str | None":
    if not isinstance(new, dict):
        return None
    if id(new) in OUTSIDE and OUTSIDE[id(new)][0] is new:
        return "returned a plan read outside the door"
    for key in ("weeks", "goal"):
        v = new.get(key)
        if v is None:
            continue
        for obj, r in OUTSIDE.values():
            if obj.get(key) is v:
                return f"carries the {key} of a plan read outside the door ({r})"
    return None


def _audit_mutator(mutate):
    def m(plan, *a, **k):
        new = mutate(plan, *a, **k)
        MUT.append({"route": STATE["route"],
                    "kind": "none" if new is None else "same" if new is plan else "other",
                    "stale": _stale(new) if new is not plan else None})
        return new
    return m


if PS is not None:
    _orig_update = PS.update

    def _audited_update(profile_id, mutate, *a, **k):
        return _orig_update(profile_id, _audit_mutator(mutate), *a, **k)
    PS.update = _audited_update
elif hasattr(tp, "update_plan"):
    _orig_update_plan = tp.update_plan

    def _audited_update_plan(json_path, mutate):
        return _orig_update_plan(json_path, _audit_mutator(mutate))
    tp.update_plan = _audited_update_plan

ENRICH_KEYS = {"card_state", "card_state_v2", "content_class", "display_name", "zone_dist",
               "score", "protocol", "zwo_tss", "zwo_duration_min", "tss_budget", "ctl_drift",
               "weekly_xss_total", "on_record", "slot_type", "zone_minutes"}


def _keys(plan):
    ks = set(plan.keys()) if isinstance(plan, dict) else set()
    for w in (plan or {}).get("weeks", []) or []:
        ks |= {f"week.{k}" for k in w}
        for s in w.get("sessions", []) or []:
            ks |= {f"session.{k}" for k in s}
    return ks


INFO: dict = {"calls": [], "boots": {}}


def dump():
    INFO["refusals"] = list(PS.REFUSALS) if PS is not None and hasattr(PS, "REFUSALS") else []
    OUT.write_text(json.dumps({"records": REC, "mutators": MUT, **INFO}, indent=1, default=str))


def _refusals() -> int:
    return len(PS.REFUSALS) if PS is not None and hasattr(PS, "REFUSALS") else 0


def probe_write(p: Path, data: bytes):
    STATE["probe"] = True
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    finally:
        STATE["probe"] = False


def probe_switch(pm, pid):
    STATE["probe"] = True
    try:
        pm.switch(pid)
    finally:
        STATE["probe"] = False


def plan_path(pid) -> Path:
    return DH / "profiles" / pid / "plans" / "current_plan.json"


def stored(pid):
    STATE["probe"] = True
    try:
        return json.loads(plan_path(pid).read_text())
    except (OSError, ValueError):
        return None
    finally:
        STATE["probe"] = False


# Rides for the adaptation paths (inputs only): seven identical daily loads
# (test_340_continuous_w2._monotone_rides).
RIDES = [{"started_at": f"{(TODAY - timedelta(days=i)).isoformat()}T10:00:00",
          "tss": 70, "time_in_zone": {"z1": 600, "z2": 2400}} for i in range(7)]
A._load_all_rides_safe = lambda *a, **k: list(RIDES)
try:
    import ride_storage as _rs
    _rs.ride_record = lambda *a, **k: list(RIDES)
except Exception:  # noqa: BLE001
    pass

# ── the plants: the self-test (--plant) ──────────────────────────────────────
if PLANT:
    def _pid():
        return ProfileManager.get().active_id

    def _raw_path():
        return plan_path(_pid())

    @A.app.post("/api/zz_plant_direct")
    def _zz_plant_direct():
        plan = stored(_pid())
        plan["zz"] = 1
        if PS is not None:
            PS._commit(PS._paths(_pid()), plan)              # PLANT: the private writer
        else:
            tp.atomic_write_plan(_raw_path(), plan)
        return {"ok": True}

    @A.app.post("/api/zz_plant_raw")
    def _zz_plant_raw():
        plan = stored(_pid())
        with open(_raw_path(), "w") as f:                    # PLANT: raw write
            json.dump(plan, f)
        return {"ok": True}

    @A.app.post("/api/zz_plant_stale")
    def _zz_plant_stale():
        if PS is not None:
            plan = PS.read(_pid())                           # read OUTSIDE the door
            plan["zz_stale"] = 1
            PS.update(_pid(), lambda fresh: plan, reason="plant")
        else:
            plan = A._read_stored_plan()
            plan["zz_stale"] = 1
            tp.update_plan(_raw_path(), lambda fresh: plan)
        return {"ok": True}

    @A.app.post("/api/zz_plant_nested")
    def _zz_plant_nested():
        def m(fresh):
            if PS is not None:
                PS._commit(PS._paths(_pid()), {**fresh, "zz_nested": 1})   # PLANT: write inside a mutator
            else:
                tp.atomic_write_plan(_raw_path(), {**fresh, "zz_nested": 1})
            return None
        if PS is not None:
            PS.update(_pid(), m, reason="plant")
        else:
            tp.update_plan(_raw_path(), m)
        return {"ok": True}

    @A.app.post("/api/zz_plant_ok")
    def _zz_plant_ok():
        if PS is not None:
            PS.update(_pid(), lambda fresh: {**fresh, "zz_ok": 1}, reason="plant")   # NEGATIVE control
        else:
            tp.update_plan(_raw_path(), lambda fresh: {**fresh, "zz_ok": 1})
        return {"ok": True}


FILES = {"/api/ride/import": {"fit_file": ("probe.fit", b"\x0e\x10" + b"\0" * 62)},
         "/api/wellness/import-hrv4training": {"file": ("probe.csv", b"date,rMSSD\n2026-09-22,50\n")}}


def _call(c, m, url, body, path=None):
    if path in FILES:
        return c.post(url, files=FILES[path])
    if m == "GET":
        return c.get(url)
    if m == "DELETE":
        return c.delete(url)
    if m == "PUT":
        return c.put(url, json=body if body is not None else {"name": "renamed"})
    return c.post(url, json=body if body is not None else {})


def main():
    # boot0: a legacy single-user home (root plans/, no profiles yet) -> the
    # profile migration copies it into profiles/default/plans.
    legacy = {"goal": {"goal_type": "general"}, "weeks": [], "generated": "2026-01-01T00:00:00"}
    probe_write(DH / "plans" / "current_plan.json", json.dumps(legacy).encode())
    probe_write(DH / "user_prefs.json", b"{}")
    STATE["route"] = "boot0 (migration of a legacy home)"
    with TestClient(A.app) as c:
        pm = ProfileManager.get()
        STATE["route"] = "setup:profile"
        if pm.active_id is None:
            probe_switch(pm, pm.create_profile("rider"))
        pid_a = pm.active_id
        INFO["boots"]["boot0_migrated"] = plan_path(pid_a).exists()
        # a latch, so this legacy root is not adopted again by later boots
        INFO["boots"]["boot0_root_retired"] = (DH / "plans" / "current_plan.json.pre-v3").exists()
        STATE["probe"] = True
        try:
            if (DH / "plans" / "current_plan.json").exists():
                (DH / "plans" / "current_plan.json").replace(DH / "plans" / "current_plan.json.pre-v3")
        finally:
            STATE["probe"] = False
        STATE["route"] = "POST /api/plan/generate (seed)"
        gen_body = {"goal": "event", "event_date": (TODAY + timedelta(days=70)).isoformat(),
                    "event_name": "Probe GF", "event_km": 120, "event_climb": 1500,
                    "event_type": "granfondo", "hours_per_week": 8, "rest_days": [0, 4],
                    "max_weekday": 1.5, "max_weekend": 3.5}
        r = c.post("/api/plan/generate", json=gen_body)
        INFO["seed_generate"] = [r.status_code, r.text[:300] if r.status_code != 200 else "ok"]
        seed = plan_path(pid_a).read_bytes()
        seed_keys = _keys(json.loads(seed))
        plan0 = json.loads(seed)
        wk = next(w for w in plan0["weeks"] if w["start"] <= TODAY.isoformat() <= w["end"])
        pend = [s for s in wk["sessions"] if s["day"] >= TODAY.isoformat() and s["session_type"] != "rest"]
        d1 = pend[0]["day"] if pend else TODAY.isoformat()
        other = [s["day"] for s in wk["sessions"] if s["day"] > TODAY.isoformat() and s["day"] != d1]
        d2 = other[-1] if other else d1
        lib = tp.load_workout_library()
        f0 = lib[0]["File"] if lib else "x.zwo"
        cat0 = (lib[0].get("Category") or "x") if lib else "x"
        INFO["dates"] = {"d1": d1, "d2": d2, "file": f0}
        spare = pm.create_profile("spare")
        BODIES = {
            "/api/plan/move-session": {"date": d1, "new_date": d2},
            "/api/plan/swap-type": {"date": d1, "session_type": "z2", "duration_min": 60},
            "/api/plan/dismiss-session": {"date": d1},
            "/api/plan/ftp-test-type": {"date": d1, "test_type": "ramp"},
            "/api/plan/re-draw": {"date": d1},
            "/api/plan/preview-redraw": {"date": d1},
            "/api/plan/accept-redraw": {"date": d1, "zwo_file": f0},
            "/api/plan/add-race": {"date": (TODAY + timedelta(days=40)).isoformat()},
            "/api/plan/mark-unavailable": {"start": d2, "end": d2, "type": "illness", "reason": "probe"},
            "/api/plan/save-availability": {"availability": {d2: {"hours": 0, "type": "rest"}}},
            "/api/plan/generate": gen_body,
            "/api/plan/auto-adjust": {"scope": "today", "dry_run": False},
            "/api/readiness/apply-tier-down": {"date": d1},
            "/api/readiness/revert-cap": {"signal": "dfa"},
            "/api/today-session/persist": {"reason": "probe", "session_type": "z2", "duration_min": 45,
                                           "tss_estimate": 30, "description": "probe"},
            "/api/plan/continuous/deload-revert": {},
            "/api/daily-log": {"date": TODAY.isoformat(), "sleep_quality": 2, "fatigue": 6, "soreness": 6,
                               "stress": 6, "mood": 2, "readiness_to_train": 2},
            "/api/metrics/log": {"date": TODAY.isoformat(), "metric": "weight", "value": 70},
            "/api/profile/update-ftp": {"ftp": 260, "method": "manual", "source": "manual"},
            "/api/settings": {"target_mode": "power"},
            "/api/icu/push": {},
            "/api/calendar/push-workout": {"source": "library", "zwo_file": f0, "date": d2},
            "/api/profiles/create": {"name": "made-by-route"},
            "/api/profiles/switch": {"id": pid_a},
            "/api/setup/save": {"hours_per_week": 8},
            "/api/wellness/manual-hrv": {"date": TODAY.isoformat(), "rmssd": 50},
            "/api/diag/frontend-error": {"code": "E_PROBE"},
            "/api/ride/{ride_id}/rpe": {"rpe": 5},
            "/api/ride/{ride_id}/ftp-test-review": {"action": "reject"},
            "/api/activity/{activity_id}/race": {"is_race": False},
            "/api/workouts/bulk-segments": {"files": [f0]},
            "/api/routes/suggest": {"distance_km": 50},
        }
        # DELETE /api/profiles/{id} removes a profile that holds a plan
        probe_write(plan_path(spare), seed)
        QUERY = {"/api/plan/rematch": "?apply=1", "/api/rides/sync": "?force=1",
                 "/api/plan/update": "?debounce=0"}
        PP = {"profile_id": spare, "filename": f0, "category": cat0, "ride_id": "probe-ride",
              "activity_id": "probe-act", "region": "x", "task_id": "x"}

        # The state a conditional writer needs to reach its write: without
        # it the route answers "nothing to do" and the sweep sees no write.
        def _day(plan, day):
            return next(s for w in plan["weeks"] for s in w["sessions"] if s["day"] == day)

        def _ftp_test(plan):
            _day(plan, d1)["session_type"] = "ftp_test"

        def _hard(plan):
            _day(plan, d1)["session_type"] = "threshold"

        def _adapted_today(plan):
            s = _day(plan, TODAY.isoformat())
            s["pre_adapt"] = {k: s.get(k) for k in ("session_type", "duration_min", "tss_estimate",
                                                   "zwo_file", "zwo_name", "description")}
            s.update(adapted=True, adapted_reason="probe", session_type="recovery", duration_min=30)

        def _deload(plan):
            w = next(w for w in plan["weeks"] if w["start"] <= TODAY.isoformat() <= w["end"])
            plan["deload_advance"] = {"week_num": w["week_num"], "applied_on": TODAY.isoformat(),
                                      "original_week": json.loads(json.dumps(w))}
            w["is_stepback"] = True

        def _stale_recalc(plan):
            old = (clock.now() - timedelta(days=10)).isoformat()
            plan["generated"] = old
            plan["recalc_date"] = old

        TWEAKS = {"/api/plan/ftp-test-type": _ftp_test,
                  "/api/readiness/apply-tier-down": _hard,
                  "/api/readiness/revert-cap": _adapted_today,
                  "/api/plan/continuous/deload-revert": _deload,
                  "/api/plan/auto-recalc": _stale_recalc}

        def _seed_for(path):
            if path not in TWEAKS:
                return seed
            plan = json.loads(seed)
            TWEAKS[path](plan)
            return json.dumps(plan, indent=2).encode()

        routes = []
        for rt in A.app.routes:
            if isinstance(rt, APIRoute) and not rt.path.startswith("/api/zz_plant"):
                routes.append({"path": rt.path, "methods": sorted(rt.methods)})
        INFO["routes"] = routes
        order = sorted(routes, key=lambda x: (0 if set(x["methods"]) <= {"GET", "HEAD"} else
                                               2 if "DELETE" in x["methods"] else 1, x["path"]))
        if PLANT:
            order = [{"path": p, "methods": ["POST"]} for p in
                     ("/api/zz_plant_direct", "/api/zz_plant_raw", "/api/zz_plant_stale",
                      "/api/zz_plant_nested", "/api/zz_plant_ok")] + order
        for rt in order:
            for m in rt["methods"]:
                path = rt["path"]
                if m == "HEAD" or path in ("/", "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"):
                    continue
                url = path
                for k, v in PP.items():
                    url = url.replace("{" + k + "}", v)
                if "{" in url:
                    url = url.split("{")[0] + "x"
                url += QUERY.get(path, "")
                if pm.active_id != pid_a:
                    probe_switch(pm, pid_a)
                probe_write(plan_path(pid_a), _seed_for(path))
                if hasattr(A, "_PLAN_HEAL_SEEN"):
                    A._PLAN_HEAL_SEEN.clear()
                STATE["route"] = f"{m} {path}"
                n0, r0 = len(REC), _refusals()
                t0 = time.monotonic()
                try:
                    code = _call(c, m, url, BODIES.get(path), path).status_code
                except Exception as e:  # noqa: BLE001
                    code = f"EXC {type(e).__name__}: {str(e)[:160]}"
                time.sleep(0.05)
                after = stored(pid_a)
                new_keys = sorted(k for k in (_keys(after) - seed_keys)
                                  if k.split(".")[-1] in ENRICH_KEYS) if after else []
                INFO["calls"].append({"route": STATE["route"], "url": url, "status": code,
                                      "secs": round(time.monotonic() - t0, 2),
                                      "writes": len(REC) - n0, "refusals": _refusals() - r0,
                                      "plan_changed": (plan_path(pid_a).read_bytes() != seed)
                                      if plan_path(pid_a).exists() else "GONE",
                                      "enrichment_persisted": new_keys})
        dump()

        # ── the stack's cron sequence (cs-domestique-adapt, ADAPT_MODE=apply) ──
        probe_write(plan_path(pid_a), seed)
        for m, u, b in (("GET", "/api/version", None), ("GET", "/api/icu/connection", None),
                        ("POST", "/api/sync", None), ("POST", "/api/rides/sync?force=1", None),
                        ("POST", "/api/plan/auto-recalc", None), ("POST", "/api/plan/reforecast", None),
                        ("POST", "/api/plan/auto-adjust", {"scope": "today", "dry_run": False}),
                        ("POST", "/api/icu/push", {})):
            STATE["route"] = f"cron {m} {u}"
            n0 = len(REC)
            r = c.get(u) if m == "GET" else c.post(u, json=b)
            INFO["calls"].append({"route": STATE["route"], "status": r.status_code, "writes": len(REC) - n0})
        # ── background hooks ──
        for name, args in (("_after_background_sync", ()), ("_icu_push_daily_from_sync", ()),
                           ("_maybe_auto_reforecast", (pid_a, 3)),
                           ("_icu_push_debounced_run", (pid_a,))):
            probe_write(plan_path(pid_a), seed)
            STATE["route"] = f"bg {name}"
            n0 = len(REC)
            try:
                getattr(A, name)(*args)
                st = "ok"
            except Exception as e:  # noqa: BLE001
                st = f"EXC {type(e).__name__}: {e}"
            INFO["calls"].append({"route": STATE["route"], "status": st, "writes": len(REC) - n0})
        dump()

        # ── profile switch: B generates; A's plan must not move ──
        probe_write(plan_path(pid_a), seed)
        a_bytes = plan_path(pid_a).read_bytes()
        STATE["route"] = "switch: create+switch B"
        rb = c.post("/api/profiles/create", json={"name": "B"})
        bid = (rb.json() or {}).get("id") or (rb.json() or {}).get("profile", {}).get("id")
        rs = c.post("/api/profiles/switch", json={"id": bid, "skip_picker": True})
        STATE["route"] = "switch: B generate"
        rg = c.post("/api/plan/generate", json=gen_body)
        INFO["switch"] = {"create": rb.status_code, "bid": bid, "switch": rs.status_code,
                          "gen_b": rg.status_code, "a_unchanged": plan_path(pid_a).read_bytes() == a_bytes,
                          "b_has_plan": plan_path(bid).exists()}
        STATE["route"] = "switch: back to A"
        c.post("/api/profiles/switch", json={"id": pid_a, "skip_picker": True})
        dump()
        probe_switch(pm, bid)          # rider B active for the next boot
        b_before = plan_path(bid).read_bytes()
        a_before = plan_path(pid_a).read_bytes()
    # ── boot passes ──
    # boot2: a legacy ROOT plan newer than the profile's, in a home of
    # several profiles -> whose plan does it land in?
    STATE["probe"] = True
    (DH / "plans" / "current_plan.json.pre-v3").unlink()
    STATE["probe"] = False
    rp = json.loads(seed)
    rp["generated"] = (clock.now() + timedelta(hours=1)).isoformat()
    rp["zz_root"] = True
    probe_write(DH / "plans" / "current_plan.json", json.dumps(rp).encode())
    STATE["route"] = "boot2 (legacy root plan, several profiles)"
    with TestClient(A.app) as c:
        INFO["boots"]["boot2_active"] = ProfileManager.get().active_id
        INFO["boots"]["boot2_B_replaced_by_root"] = plan_path(bid).read_bytes() != b_before
        INFO["boots"]["boot2_A_untouched"] = plan_path(pid_a).read_bytes() == a_before
    # boot3: the profile plan is zero bytes with a .bak -> restore
    probe_write(plan_path(bid).with_name("current_plan.json.bak"), seed)
    probe_write(plan_path(bid), b"")
    STATE["route"] = "boot3 (restore from .bak)"
    with TestClient(A.app) as c:
        INFO["boots"]["boot3_restored"] = plan_path(bid).stat().st_size > 0
    # boot4: a stale zwo reference -> the boot rewrite
    sp = json.loads(seed)
    for w in sp["weeks"]:
        for s in w["sessions"]:
            if s["day"] > TODAY.isoformat() and s.get("zwo_file"):
                s["zwo_file"] = "gone_z2_probe.zwo"
                break
    probe_write(plan_path(bid), json.dumps(sp).encode())
    STATE["route"] = "boot4 (stale classification rewrite)"
    n0 = len(REC)
    with TestClient(A.app) as c:
        pass
    INFO["boots"]["boot4_writes"] = len(REC) - n0
    time.sleep(1.0)
    STATE["route"] = "after"
    dump()


try:
    main()
finally:
    dump()
    STATE["probe"] = True
    shutil.rmtree(H, ignore_errors=True)
