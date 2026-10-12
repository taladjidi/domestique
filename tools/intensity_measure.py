"""Measure before switching (contract slim/9.3, I0): minutes per zone of every
library file and every session of a stored plan, by time in zone (what the
planner targets) and by the hybrid session-goal / time-in-zone count
(zones.hybrid_minutes, what the surfaces show from slim/9.3 on).

    python tools/intensity_measure.py [--plan PLAN_JSON --lib WORKOUT_DIR] [--out OUT.json]

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
import statistics
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


def _stats(xs):
    if not xs:
        return "n/a"
    q = statistics.quantiles(xs, n=10) if len(xs) > 1 else [xs[0]] * 9
    return (f"mean {statistics.fmean(xs):+.1f}, p10 {q[0]:+.1f}, median {statistics.median(xs):+.1f}, "
            f"p90 {q[8]:+.1f}, min {min(xs):+.1f}, max {max(xs):+.1f}")


def measure_library(lib: Path):
    cls = _classes(lib)
    rows = []
    for f in sorted(lib.glob("*.zwo")):
        if zwo_parse.scan_zwo(f) is None:
            continue
        cc = cls.get(f.name, "")
        g = zones.goal_zone(cc, None)
        tiz = zones.tiz_minutes(f)
        hyb = zones.hybrid_minutes(f, g)
        el = zones._workout_element(f)
        hard_off = sum(1 for s in el if s.tag == "IntervalsT"
                       and float(s.get("OffPower", 0.5)) * 100 >= zones._WORK_PCT)
        rows.append({"file": f.name, "class": cc, "goal": g, "tiz": tiz, "hybrid": hyb,
                     "hard_off_legs": hard_off})
    return rows


def report_library(rows) -> str:
    out = ["## Library, as written", "",
           f"{len(rows)} files. hybrid minus time in zone, minutes per file, by goal zone.", ""]
    by_goal = defaultdict(list)
    for r in rows:
        by_goal[r["goal"]].append(r)
    for g in (1, 2, 3, None):
        rs = by_goal.get(g, [])
        out.append(f"### goal zone {g} ({len(rs)} files)")
        for z in ZS:
            out.append(f"- {z}: {_stats([r['hybrid'][z] - r['tiz'][z] for r in rs])}")
        tot_t = {z: sum(r["tiz"][z] for r in rs) for z in ZS}
        tot_h = {z: sum(r["hybrid"][z] for r in rs) for z in ZS}
        uncounted = sum(sum(r["tiz"].values()) - sum(r["hybrid"].values()) for r in rs)
        out.append(f"- library totals, time in zone {_fmt(tot_t)}; hybrid {_fmt(tot_h)}; "
                   f"uncounted (power 0) {uncounted:.0f} min")
        out.append("")
    n_off = sum(1 for r in rows if r["hard_off_legs"])
    out.append(f"Files with an IntervalsT off-leg at or above 76% FTP (a work bout, counted "
               f"to the goal zone since the 2026-10-12 amendment): {n_off}")
    out.append("")
    return "\n".join(out)


def _fmt(d):
    tot = sum(d.values()) or 1
    return " / ".join(f"{d[z]:.0f} ({100 * d[z] / tot:.1f}%)" for z in ZS)


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
            g = zones.goal_zone(cls.get(zf), st)
            wk["sessions"].append({"day": s.get("day"), "type": st, "file": zf, "goal": g,
                                   "tiz": zones.tiz_minutes(b), "hybrid": zones.hybrid_minutes(b, g)})
        weeks.append(wk)
    return weeks


def report_plan(weeks) -> str:
    out = ["## The plan, as served", "",
           "| week | sessions | time in zone z1/z2/z3 | hybrid z1/z2/z3 | hybrid - TIZ |",
           "|---|---|---|---|---|"]
    for wk in weeks:
        t = {z: sum(s["tiz"][z] for s in wk["sessions"]) for z in ZS}
        h = {z: sum(s["hybrid"][z] for s in wk["sessions"]) for z in ZS}
        out.append(f"| {wk['start']}..{wk['end']} | {len(wk['sessions'])} | "
                   + " / ".join(f"{t[z]:.0f}" for z in ZS) + " | "
                   + " / ".join(f"{h[z]:.0f}" for z in ZS) + " | "
                   + " / ".join(f"{h[z] - t[z]:+.0f}" for z in ZS) + " |")
    out += ["", "Per session:", "", "| day | type | goal | TIZ z1/z2/z3 | hybrid z1/z2/z3 | file |",
            "|---|---|---|---|---|---|"]
    for wk in weeks:
        for s in wk["sessions"]:
            out.append(f"| {s['day']} | {s['type']} | {s['goal']} | "
                       + " / ".join(f"{s['tiz'][z]:.0f}" for z in ZS) + " | "
                       + " / ".join(f"{s['hybrid'][z]:.0f}" for z in ZS) + f" | {s['file']} |")
    out.append("")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", default=str(SRC / "workouts"))
    ap.add_argument("--plan")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    lib = Path(a.lib)
    bad = check_tiz(lib)
    print(f"# Intensity: time in zone vs hybrid\n\nInstrument: zones.tiz_minutes vs "
          f"zwo_parse.scan_zwo, {bad} files differ.\n")
    if bad:
        return 1
    rows = measure_library(lib)
    print(report_library(rows))
    weeks = measure_plan(Path(a.plan), lib) if a.plan else []
    if weeks:
        print(report_plan(weeks))
    if a.out:
        Path(a.out).write_text(json.dumps({"library": rows, "plan": weeks}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
