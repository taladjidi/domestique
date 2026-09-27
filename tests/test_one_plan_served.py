"""G5 (contract 10.5/10.6): what the rider is served is the stored plan.

G1-G4 see who writes the plan; they cannot see a GET that serves an answer
the plan does not hold. For every plan-returning GET, each served session's
(day, duration, TSS, file) equals the stored plan's; the type too where the
route serves the slot's type (/api/plan, /api/today-session). The week view
(/api/weekly-plan, /api/calendar) serves the type of the FILE's content
(week-view contract A2, decided 2026-09-14: the Today card may not say
SWEETSPOT over a threshold file), so there the type is the stored file's,
checked through the same classifier, not the slot label. Week totals equal
the stored sessions' sums.

And: planner builders reachable from a GET handler are an allowlist, each a
labelled draft or a figure that is not a session.
"""
from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import clock
import plan_seam
import plan_store

GEN = {"goal": "general", "hours_per_week": 7.0, "plan_weeks": 6, "rest_days": [0]}
TODAY = date(2026, 9, 23)


@pytest.fixture
def served(tmp_path, monkeypatch, freeze_clock):
    freeze_clock(TODAY)
    plan_seam.bind_active(tmp_path)
    monkeypatch.setattr(app_module, "get_today_metrics", lambda *a, **k: {})
    c = TestClient(app_module.app)
    assert c.post("/api/plan/generate", json=GEN).status_code == 200
    return c


def _stored():
    plan = plan_store.read(app_module._pid())
    return {s["day"]: s for w in plan["weeks"] for s in w["sessions"]}, plan


def _key(s, tss="tss_estimate"):
    return (int(s.get("duration_min") or 0), round(float(s.get(tss) or 0)), s.get("zwo_file") or "")


def test_the_plan_tab_serves_the_stored_sessions(served):
    stored, _ = _stored()
    js = served.get("/api/plan").json()["plan_json"]
    got = {s["day"]: s for w in js["weeks"] for s in w["sessions"]}
    assert got.keys() == stored.keys()
    for d, s in stored.items():
        assert (got[d]["session_type"], *_key(got[d])) == (s["session_type"], *_key(s)), d


def test_the_week_view_serves_the_stored_sessions(served):
    stored, _ = _stored()
    wk = served.get("/api/weekly-plan").json()
    on = [s for s in wk["sessions"] if s.get("on_record")]
    assert on
    for s in on:
        assert _key(s) == _key(stored[s["day"]]), s["day"]
    assert wk["planned_tss"] == pytest.approx(sum(
        float(stored[s["day"]].get("tss_estimate") or 0) for s in on), abs=1.0)


def test_the_calendar_serves_the_stored_sessions(served):
    stored, _ = _stored()
    cal = served.get("/api/calendar").json()
    n = 0
    for w in cal["weeks"]:
        tot = 0.0
        for d in w["days"]:
            p = d.get("planned")
            if not p:
                continue
            s = stored[d["date"]]
            assert (int(p["duration_min"]), round(float(p["tss"])), p["zwo_file"] or "") \
                == _key(s), d["date"]
            tot += float(s.get("tss_estimate") or 0)
            n += 1
        assert (w["planned_tss"] or 0.0) == pytest.approx(tot, abs=1.0), w["start_date"]
    assert n > 10


def test_the_week_summary_totals_the_stored_week(served):
    stored, _ = _stored()
    ws = served.get("/api/week-summary").json()
    days = {d for d in stored if ws["week_start"] <= d <= ws["week_end"]}
    assert ws["planned_tss"] == pytest.approx(sum(
        float(stored[d].get("tss_estimate") or 0) for d in days), abs=1.0)


def test_today_serves_the_stored_session(served):
    stored, _ = _stored()
    t = served.get("/api/today-session").json()
    s = stored[TODAY.isoformat()]
    a = t["adjusted"]
    assert (a["session_type"], int(a["duration_min"]), round(float(a["tss_estimate"])),
            a.get("zwo_file") or "") == (s["session_type"], *_key(s))


# ── the readiness adjustment is a plan write (10.5, the audit's D1) ─────────
def _bad_morning(c):
    return c.post("/api/daily-log", json={"date": TODAY.isoformat(), "soreness": 7, "fatigue": 7,
                                          "stress": 6, "sleep_quality": 6, "mood": 5})


def test_a_readiness_adjustment_is_stored_and_served(served, monkeypatch):
    monkeypatch.setattr(app_module, "_is_readiness_cap_reverted_today", lambda: False)
    before, _ = _stored()
    orig = before[TODAY.isoformat()]
    r = _bad_morning(served)
    assert r.status_code == 200
    after, _ = _stored()
    s = after[TODAY.isoformat()]
    t = served.get("/api/today-session").json()
    assert t["was_modified"], "the fixture's morning does not call for an adjustment"
    assert s.get("adapted") and s.get("adapted_by") == "readiness"
    a = t["adjusted"]
    assert (a["session_type"], int(a["duration_min"]), round(float(a["tss_estimate"])),
            a.get("zwo_file") or "") == (s["session_type"], *_key(s))
    assert (t["planned"]["session_type"], int(t["planned"]["duration_min"])) == \
        (orig["session_type"], int(orig["duration_min"]))
    v = plan_store.version(app_module._pid())
    assert _bad_morning(served).status_code == 200            # idempotent
    assert plan_store.version(app_module._pid()) == v


def test_the_revert_restores_the_stored_prescription(served, monkeypatch):
    monkeypatch.setattr(app_module, "_is_readiness_cap_reverted_today", lambda: False)
    before, _ = _stored()
    orig = before[TODAY.isoformat()]
    _bad_morning(served)
    monkeypatch.undo()
    monkeypatch.setattr(app_module, "get_today_metrics", lambda *a, **k: {})
    assert served.post("/api/readiness/revert-cap", json={"signal": "dfa"}).status_code == 200
    s = _stored()[0][TODAY.isoformat()]
    assert (s["session_type"], *_key(s)) == (orig["session_type"], *_key(orig))
    _bad_morning(served)                                       # the revert holds today
    s = _stored()[0][TODAY.isoformat()]
    assert (s["session_type"], *_key(s)) == (orig["session_type"], *_key(orig))


# ── what a GET may build ─────────────────────────────────────────────────────
BUILDERS = {"generate_plan", "generate_phases", "finalize_weeks", "reforecast", "reforecast_dict",
            "recalculate_plan", "regenerate_from_today", "extend_plan", "match_zwo",
            "daily_adapt_plan", "_apply_plan_update", "adjust_today_session", "plan_week",
            "_row_budget", "scale_budget_to_week", "holdable_tss", "_continuous_weekly_tss",
            "apply_week_tier_down", "budget_table"}
# GET route -> builders it may reach, and why that is not a second plan.
DRAFTS = {
    "/api/plan/preview": ({"generate_phases", "_row_budget", "budget_table", "holdable_tss",
                           "scale_budget_to_week", "_continuous_weekly_tss"},
                          "the stored phases while the form equals the stored goal; otherwise "
                          "labelled draft (source: 'draft')"),
    "/api/plan/entry-scan": ({"generate_phases", "_row_budget", "budget_table", "holdable_tss",
                              "scale_budget_to_week", "_continuous_weekly_tss"},
                             "an entry-credit proposal for the form (no sessions); it becomes "
                             "real only through Generate"),
    "/api/plan/daily-adapt": (BUILDERS,
                              "a dry run of the on-sync adaptation (_apply_plan_update and all "
                              "it calls) on a copy of the stored plan (projection_only)"),
    "/api/calendar": ({"budget_table"}, "the phase's zone-mix target (target_polarized), "
                                        "a property of the goal, not of a session"),
}


def _reach():
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    import plan_scan as S
    src = Path(app_module.__file__).parent
    mods = S.load(src)
    S.index(mods)
    edges = {}
    for m in mods.values():
        for q, fn in m.funcs.items():
            e = set()
            for n in S.own_nodes(fn):
                if isinstance(n, ast.Call):
                    t = S.resolve_ref(mods, m, n.func, q)
                    if t:
                        e.add(t)
                    for a in list(n.args) + [k.value for k in n.keywords]:
                        if isinstance(a, (ast.Name, ast.Attribute)):
                            r = S.resolve_ref(mods, m, a, q)
                            if r:
                                e.add(r)
            e |= {q2 for q2 in m.funcs if q2.startswith(q + ".")}
            edges[q] = e
    out = {}
    for q, fn in mods["app"].funcs.items():
        for d in fn.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr == "get" and d.args and isinstance(d.args[0], ast.Constant)):
                seen, st = {q}, [q]
                while st:
                    c = st.pop()
                    for x in edges.get(c, ()):
                        if x not in seen:
                            seen.add(x)
                            st.append(x)
                hit = {s.split(":")[1].split(".")[-1] for s in seen} & BUILDERS
                if hit:
                    out[d.args[0].value] = hit
    return out


def test_a_get_builds_only_labelled_drafts():
    reach = _reach()
    bad = {r: sorted(h - DRAFTS.get(r, (set(), ""))[0]) for r, h in reach.items()
           if h - DRAFTS.get(r, (set(), ""))[0]}
    assert bad == {}, bad


# ── the configurator's preview (10.5, the audit's D3) ────────────────────────
def test_the_preview_of_the_stored_goal_is_the_stored_phases(tmp_path, monkeypatch, freeze_clock):
    freeze_clock(TODAY)
    plan_seam.bind_active(tmp_path)
    monkeypatch.setattr(app_module, "get_today_metrics", lambda *a, **k: {})
    c = TestClient(app_module.app)
    ev = (TODAY + timedelta(days=70)).isoformat()
    assert c.post("/api/plan/generate", json={"goal": "event", "event_date": ev,
                                              "hours_per_week": 8, "rest_days": [0]}).status_code == 200
    stored = [(p["name"], p["weeks"], p["weekly_tss"]) for p in _stored()[1]["phases"]]
    real = app_module._planning_ctl
    monkeypatch.setattr(app_module, "_planning_ctl", lambda: real() + 12.0)   # fitness moved
    q = {"goal": "event", "event_date": ev}
    pv = c.get("/api/plan/preview", params=q).json()
    assert pv["source"] == "stored" and not pv["draft"]
    assert [(p["name"], p["weeks"], p["weekly_tss"]) for p in pv["phases"]] == stored
    other = c.get("/api/plan/preview", params={"goal": "event",
                                               "event_date": (TODAY + timedelta(days=84)).isoformat()}).json()
    assert other["source"] == "draft" and other["draft"]
