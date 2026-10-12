"""Measure (contract slim/9.3, I0): minutes per zone of every library file and
every session of a stored plan, by time in zone (zones.tiz_minutes), which is
both what the planner targets and, since the owner's amendment 2, what every
surface shows for a planned workout (the hybrid is kept for heart-rate-only
rides, zones.hr_hybrid_minutes; there is no plan-side hybrid any more).

    python tools/intensity_measure.py [--plan PLAN_JSON --lib WORKOUT_DIR] [--out OUT.json] [--file NAME ...]

The library is read as written. A plan's sessions are read as served
(download_lib.served_zwo_bytes, fitted to the slot, no capacity cap). Prints a
markdown report; --out keeps the per-file rows.

The time in zone is checked against zwo_parse.scan_zwo for every library file
before anything is reported: the counter that disagrees with the planner's own
scanner on one file is not measuring the planner's targets.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import zones  # noqa: E402
import zwo_parse  # noqa: E402

ZS = ("z1", "z2", "z3")


def _classes(lib: Path) -> dict:
    try:
        c = json.loads((lib / ".content_classification.json").read_text())["classifications"]
    except (OSError, ValueError, KeyError):
        c = json.loads((SRC / "workouts" / ".content_classification.json").read_text())["classifications"]
    return {k: (v.get("primary") or "") for k, v in c.items()}


def check_tiz(lib: Path) -> int:
    """Files whose zones.tiz_minutes differs from scan_zwo's zone seconds."""
    bad = 0
    for f in sorted(lib.glob("*.zwo")):
        r = zwo_parse.scan_zwo(f)
        if r is None:
            continue
        ref = {"z1": (r["z1_sec"] + r["z2_sec"]) / 60, "z2": (r["z3_sec"] + r["z4_sec"]) / 60,
               "z3": (r["z5_sec"] + r["z6_sec"]) / 60}
        t = zones.tiz_minutes(f)
        if any(abs(t[k] - ref[k]) > 1e-6 for k in ZS):
            bad += 1
    return bad


def measure_library(lib: Path):
    cls = _classes(lib)
    rows = []
    for f in sorted(lib.glob("*.zwo")):
        if zwo_parse.scan_zwo(f) is None:
            continue
        cc = cls.get(f.name, "")
        rows.append({"file": f.name, "class": cc, "goal": zones.goal_zone(cc, None),
                     "tiz": zones.tiz_minutes(f)})
    return rows


def _fmt(d):
    tot = sum(d.values()) or 1
    return " / ".join(f"{d[z]:.0f} ({100 * d[z] / tot:.1f}%)" for z in ZS)


def report_library(rows) -> str:
    out = ["## Library, as written", "",
           f"{len(rows)} files, minutes per zone by time in zone, totals by goal zone.", ""]
    by_goal = defaultdict(list)
    for r in rows:
        by_goal[r["goal"]].append(r)
    for g in (1, 2, 3, None):
        rs = by_goal.get(g, [])
        tot = {z: sum(r["tiz"][z] for r in rs) for z in ZS}
        out.append(f"- goal zone {g} ({len(rs)} files): {_fmt(tot)}")
    out.append("")
    return "\n".join(out)


def measure_plan(plan_path: Path, lib: Path):
    import download_lib as dl
    cls = _classes(lib)
    plan = json.loads(plan_path.read_text())
    weeks = []
    for w in plan.get("weeks") or []:
        wk = {"start": w.get("start"), "end": w.get("end"), "sessions": []}
        for s in w.get("sessions") or []:
            st = (s.get("session_type") or "rest").lower()
            zf = s.get("zwo_file") or ""
            if st == "rest" or not zf or not (lib / zf).exists():
                continue
            b = dl.served_zwo_bytes(lib / zf, s.get("duration_min"), st, None, cap=False)
            wk["sessions"].append({"day": s.get("day"), "type": st, "file": zf,
                                   "goal": zones.goal_zone(cls.get(zf), st),
                                   "tiz": zones.tiz_minutes(b)})
        weeks.append(wk)
    return weeks


def report_plan(weeks) -> str:
    out = ["## The plan, as served (time in zone)", "",
           "| week | sessions | z1 / z2 / z3 |", "|---|---|---|"]
    for wk in weeks:
        t = {z: sum(s["tiz"][z] for s in wk["sessions"]) for z in ZS}
        out.append(f"| {wk['start']}..{wk['end']} | {len(wk['sessions'])} | "
                   + " / ".join(f"{t[z]:.0f}" for z in ZS) + " |")
    out += ["", "| day | type | goal | z1 / z2 / z3 | file |", "|---|---|---|---|---|"]
    for wk in weeks:
        for s in wk["sessions"]:
            out.append(f"| {s['day']} | {s['type']} | {s['goal']} | "
                       + " / ".join(f"{s['tiz'][z]:.1f}" for z in ZS) + f" | {s['file']} |")
    out.append("")
    return "\n".join(out)


def report_files(lib: Path, names) -> str:
    out = ["## Named files (time in zone)", ""]
    for n in names:
        t = zones.tiz_minutes(lib / n)
        out.append(f"- {n}: " + " / ".join(f"{t[z]:.1f}" for z in ZS) + " min")
    out.append("")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", default=str(SRC / "workouts"))
    ap.add_argument("--plan")
    ap.add_argument("--out")
    ap.add_argument("--file", action="append", default=[])
    a = ap.parse_args(argv)
    lib = Path(a.lib)
    bad = check_tiz(lib)
    print(f"# Intensity: time in zone\n\nInstrument: zones.tiz_minutes vs "
          f"zwo_parse.scan_zwo, {bad} files differ.\n")
    if bad:
        return 1
    rows = measure_library(lib)
    print(report_library(rows))
    weeks = measure_plan(Path(a.plan), lib) if a.plan else []
    if weeks:
        print(report_plan(weeks))
    if a.file:
        print(report_files(lib, a.file))
    if a.out:
        Path(a.out).write_text(json.dumps({"library": rows, "plan": weeks}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
