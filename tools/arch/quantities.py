#!/usr/bin/env python3
"""Does every endpoint give the same answer to the same question?

    python3 tools/arch/quantities.py [--json] [--quantity ctl]

WHY THIS EXISTS. The bugs that cost this project the most were never wrong
formulas. They were two correct formulas: CTL merged per-field from
intervals.icu and a local EWMA, so the readiness card and the calendar
disagreed; a ride's duration meaning elapsed in one reader and moving in
another, so the same ride read 118 minutes and 91; a week's TSS target
counting the sessions still in the plan while the ride total counted the whole
calendar week, so a mid-week replan read 499/138 where intervals.icu read
499/387. Each was found by a rider noticing two screens disagree.

So this asks the running app the same question through every door. It boots
the app over ONE fixture -- one plan, one set of rides, one wellness history --
calls every GET route, walks the JSON, and files every number under the
quantity it answers. A quantity answered two ways is the finding, and the
report names both endpoints and both values.

WHAT IT IS NOT. It does not read the source, so it cannot tell you WHY two
endpoints disagree; it tells you THAT they do, which is the part a reader
cannot talk themselves out of. It probes GET only: a POST would change the
fixture underneath the comparison.

THE CONTROL. `--self-test` injects a disagreement (one endpoint's fitness
state is shifted by a known amount) and fails unless the probe reports it. An
agreement report from a probe that cannot detect disagreement is worthless,
and this one has been wrong that way before: the first version compared only
keys named exactly "ctl" and passed while `ctl_actual` disagreed beside it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

# A quantity is a question the app answers. The aliases are the key names the
# responses use for it -- gathered by reading the responses, not guessed: the
# same number travels as `ctl`, `ctl_actual` and `current_ctl` depending on
# which endpoint hands it over.
QUANTITIES: dict[str, dict] = {
    "ctl": {
        "keys": ("ctl", "ctl_actual", "current_ctl"),
        "tol": 0.55,          # one decimal place, rounded independently
        "scope": "athlete",
    },
    "atl": {"keys": ("atl",), "tol": 0.55, "scope": "athlete"},
    "tsb": {"keys": ("tsb",), "tol": 0.55, "scope": "athlete"},
    "ftp": {"keys": ("ftp", "current_ftp"), "tol": 0.5, "scope": "athlete"},
    # THREE quantities, and the app has been calling them one thing. The
    # ramp's budget for the week, the load actually prescribed on its days,
    # and the load ridden. They coincide in a healthy week, which is why seven
    # key names across three endpoints went unnoticed: /api/calendar says
    # planned_tss, /api/weekly-plan says tss_target AND budget AND planned_tss
    # in one response, /api/week-summary says tss_target for a different one
    # of the three. `tss_target` is deliberately filed under the prescribed
    # load here, because that is what the rollup divides by -- so when a
    # week's budget and its sessions disagree, this reports it instead of
    # hiding it behind a shared name.
    "week_budget": {"keys": ("budget", "weekly_tss_target", "target_tss"),
                    "tol": 0.5, "scope": "week"},
    "week_prescribed": {"keys": ("tss_target", "planned_tss"),
                        "tol": 0.5, "scope": "week"},
    "week_done": {"keys": ("tss_done", "actual_tss", "tss_actual"),
                  "tol": 0.5, "scope": "week"},
    # How long the rider rode, as a reader DISPLAYS it. duration_s and
    # moving_s are deliberately not in here: a record carrying both is not a
    # disagreement, it is the raw material. What must agree is the minutes two
    # screens put in front of the rider for the same ride -- the 118-vs-91 bug.
    "ride_duration_min": {"keys": ("duration_min",), "tol": 0.6, "scope": "ride"},
    "ride_tss": {"keys": ("tss",), "tol": 0.5, "scope": "ride"},
    # What the plan asks of a given day. Three screens show today's session --
    # the today card, the week view, the calendar cell -- and they are three
    # reads of one row.
    "session_planned_tss": {"keys": ("tss_estimate", "planned_tss"),
                            "tol": 0.5, "scope": "session"},
    "session_planned_min": {"keys": ("duration_min", "planned_min"),
                            "tol": 0.5, "scope": "session"},
}
# (kind, key) -> quantity. Keyed on the SHAPE of the object the key sits in,
# because `duration_min` means the ride in a ride and the prescription in a
# session, and comparing the two is how a reader concludes the app disagrees
# with itself when it does not.
_KEY_TO_QUANTITY = {(spec["scope"], k): q
                    for q, spec in QUANTITIES.items() for k in spec["keys"]}
# Keys that identify which ride an object describes, for ride-scoped values.
_RIDE_ID_KEYS = ("ride_id", "activity_id", "external_id", "id")
# Keys that say WHEN an object is about. A time series answers the same
# question once per day, and comparing Monday's CTL with Thursday's is not a
# finding -- it is the series doing its job. The first version of this tool
# did exactly that and reported a 28.9 spread across /api/wellness.
# "generated_on" matters: a plan's ctl_snapshot is the CTL the day the plan
# was built, not today's. Without it the snapshot is compared with every
# live reader and an ordinary stale anchor reads as a disagreement.
_WHEN_KEYS = ("date", "day", "start_date_local", "as_of", "generated_on", "id")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")
# The day the probe is asking about, set by probe() from the fixture. A
# response that says nothing about when it means -- the today card, the
# readiness card -- means this one, and has to land in the same bucket as the
# endpoints that do name the date.
TODAY = [""]


class Observation:
    __slots__ = ("quantity", "value", "endpoint", "path", "scope")

    def __init__(self, quantity, value, endpoint, path, scope):
        self.quantity, self.value = quantity, value
        self.endpoint, self.path, self.scope = endpoint, path, scope

    def __repr__(self) -> str:
        return f"{self.endpoint} {self.path} = {self.value}"


def _when_of(payload: dict) -> str:
    """The day an object is about, or "" when it does not say."""
    for k in _WHEN_KEYS:
        v = payload.get(k)
        if isinstance(v, str) and _ISO_DAY.match(v):
            return v[:10]
    return ""


def _when_from_scope(scope: str) -> str:
    return scope.split("@", 1)[1] if "@" in scope else ""


# Paths whose numbers share a name with a tracked quantity while answering a
# different question. The readiness composite scores each input 0..100 and
# calls one of them `tsb`; comparing that with the real TSB is a false
# positive, and a probe that cries wolf gets muted. Kept short on purpose:
# every entry here is a hole in the comparison, so each one names its reason.
_EXCLUDED_PATHS = (
    ".components.",       # readiness sub-scores, 0..100, not the quantity
)


def _excluded(path: str) -> bool:
    return any(frag in path for frag in _EXCLUDED_PATHS)


def _kind_of(payload: dict, when: str, scope: str) -> "tuple[str, str]":
    """What an object IS, and the subject its numbers are about.

    A ride names itself with an id and carries ride facts; a session carries a
    session_type or a workout file; a week carries its sessions. Anything else
    inherits its parent's subject, so a number nested under a ride still
    belongs to that ride, and a bare number at the top of a response is about
    the athlete today.
    """
    for k in _RIDE_ID_KEYS:
        if isinstance(payload.get(k), (str, int)) and (
                "duration_min" in payload or "moving_s" in payload
                or "tss" in payload):
            return "ride", f"ride:{payload[k]}"
    if ("session_type" in payload or "zwo_file" in payload
            or "tss_estimate" in payload):
        # /api/today-session nests the prescription under `planned` with no
        # type and no date -- it is today's by definition. Without this the
        # card the rider reads first contributed one number (its FTP) and its
        # minutes and TSS were compared with nothing.
        day = when or _when_from_scope(scope) or TODAY[0]
        return ("session", f"session:{day}") if day else ("other", scope)
    if isinstance(payload.get("week_start"), str):
        return "week", f"week:{payload['week_start']}"
    if isinstance(payload.get("start"), str) and "sessions" in payload:
        return "week", f"week:{payload['start']}"
    if isinstance(payload.get("start_date"), str) and "iso_week" in payload:
        # /api/calendar's weeks. They key on start_date and iso_week, which
        # this did not recognise, so the biggest plan-versus-actual screen in
        # the app contributed ZERO week numbers to the comparison.
        return "week", f"week:{payload['start_date']}"
    if "tss_target" in payload or "tss_done" in payload:
        # A week-shaped body that names its span some other way (the rollup
        # says week_start; the weekly plan says start) still answers week
        # questions. Fall back to the window the response is about.
        return "week", f"week:{when}" if when else scope or "week:?"
    return "athlete", scope


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def walk(payload, endpoint: str, path: str = "", scope: str = "",
         when: str = "", out: "list[Observation] | None" = None) -> list:
    """Every number in ``payload`` filed under the quantity it answers.

    ``scope`` carries the subject the number is about (a ride id, a week
    start) so two endpoints are only compared when they describe the same
    thing. Without it, every ride's duration would be compared with every
    other ride's and the report would be noise.
    """
    out = [] if out is None else out
    if isinstance(payload, dict):
        # The date flows DOWN the response, it is not read off the leaf: a
        # calendar cell names its day once and nests `planned` inside it.
        # Reading the date only where the number sits put every day of every
        # week in today's bucket and reported a 157-minute "disagreement"
        # between Monday's session and Thursday's.
        when = _when_of(payload) or when
        kind, here = _kind_of(payload, when, scope)
        for k, v in payload.items():
            q = _KEY_TO_QUANTITY.get((kind, k))
            # A boxed value -- {"value": 42.0, "unit": ...} under a key that
            # names the quantity. /api/rider-stats reports load.ctl.value,
            # load.atl.value and power.ftp.value that way, so a whole reader
            # of the athlete's fitness was invisible to a leaf-name scan.
            n = _num(v)
            if n is None and isinstance(v, dict):
                n = _num(v.get("value"))
                if n is not None and q is None:
                    q = _KEY_TO_QUANTITY.get(("athlete", k))
            if q is not None and n is not None and not _excluded(f"{path}.{k}"):
                sc = (f"athlete@{when or TODAY[0] or 'today'}"
                      if QUANTITIES[q]["scope"] == "athlete" else here)
                out.append(Observation(q, n, endpoint, f"{path}.{k}", sc))
            walk(v, endpoint, f"{path}.{k}", here, when, out)
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            walk(v, endpoint, f"{path}[{i}]", scope, when, out)
    return out


def disagreements(observations: list, only: str = "") -> list[dict]:
    """Quantities answered two ways for the same subject."""
    by: dict = defaultdict(list)
    for o in observations:
        if only and o.quantity != only:
            continue
        by[(o.quantity, o.scope)].append(o)
    found = []
    for (quantity, scope), obs in sorted(by.items()):
        if len({o.endpoint for o in obs}) < 2:
            continue                           # one door, nothing to compare
        tol = QUANTITIES[quantity]["tol"]
        lo, hi = min(o.value for o in obs), max(o.value for o in obs)
        if hi - lo <= tol:
            continue
        found.append({
            "quantity": quantity, "scope": scope, "spread": round(hi - lo, 3),
            "answers": sorted(
                ({"value": o.value, "endpoint": o.endpoint, "path": o.path}
                 for o in obs), key=lambda a: a["value"]),
        })
    return found


def probe(client, routes: list, fixtures: dict) -> tuple[list, list]:
    """Call every GET route that can be addressed; return (observations, skipped)."""
    TODAY[0] = str(fixtures.get("day") or "")
    observations, skipped = [], []
    for path, name in routes:
        url, missing = path, []
        for part in path.split("/"):
            if part.startswith("{") and part.endswith("}"):
                key = part[1:-1]
                if key in fixtures:
                    url = url.replace(part, str(fixtures[key]), 1)
                else:
                    missing.append(key)
        if missing:
            skipped.append((path, f"no fixture for {', '.join(missing)}"))
            continue
        try:
            r = client.get(url)
        except Exception as e:                  # a GET that raises is a finding
            skipped.append((path, f"raised {type(e).__name__}: {e}"))
            continue
        if r.status_code != 200:
            skipped.append((path, f"HTTP {r.status_code}"))
            continue
        ctype = r.headers.get("content-type", "")
        if "json" not in ctype:
            skipped.append((path, f"not JSON ({ctype.split(';')[0]})"))
            continue
        try:
            payload = r.json()
        except ValueError:
            skipped.append((path, "undecodable JSON"))
            continue
        walk(payload, url, out=observations)
    return observations, skipped


def get_routes(app) -> list:
    out = []
    for r in app.routes:
        methods = getattr(r, "methods", set()) or set()
        if "GET" in methods and getattr(r, "path", "").startswith("/api"):
            out.append((r.path, getattr(r, "name", "")))
    return sorted(set(out))


def format_report(found: list, observations: list, skipped: list) -> str:
    lines = [f"{len(observations)} numbers from "
             f"{len({o.endpoint for o in observations})} endpoints; "
             f"{len(skipped)} endpoints not probed", ""]
    if not found:
        lines.append("No quantity is answered two ways.")
    for f in found:
        lines.append(f"{f['quantity']} ({f['scope']}): spread {f['spread']}")
        for a in f["answers"]:
            lines.append(f"    {a['value']:>12}  {a['endpoint']}{a['path']}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quantity", default="", help="only this quantity")
    ap.add_argument("--scenario", default="", help="one scenario, or all")
    ap.add_argument("--self-test", action="store_true",
                    help="inject a disagreement and fail unless it is reported")
    args = ap.parse_args()
    sys.path.insert(0, str(ROOT / "tests"))
    from quantity_fixture import build            # noqa: E402

    from quantity_fixture import SCENARIOS                 # noqa: E402
    scenarios = [args.scenario] if args.scenario else list(SCENARIOS)
    found, obs, skipped = [], [], []
    for sc in scenarios:
        with build(shift_one_endpoint=args.self_test, scenario=sc) as (client, fixtures):
            import app as app_module
            o, sk = probe(client, get_routes(app_module.app), fixtures)
        for f in disagreements(o, args.quantity):
            f["scenario"] = sc
            found.append(f)
        obs += o
        skipped += sk
        print(f"[{sc}] {len(o)} numbers from {len({x.endpoint for x in o})} endpoints")
    if args.json:
        print(json.dumps({"disagreements": found,
                          "skipped": [{"path": p, "why": w} for p, w in skipped]},
                         indent=2))
    else:
        print(format_report(found, obs, skipped))
    if args.self_test:
        ok = any(f["quantity"] == "ctl" for f in found)
        print("\nCONTROL:", "detected the injected disagreement" if ok
              else "FAILED — the probe cannot see a disagreement it was handed")
        sys.exit(0 if ok else 1)
    sys.exit(1 if found else 0)
