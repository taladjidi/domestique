"""3.13.0 — races planned in intervals.icu load into the plan builder.

GET /api/icu/races reads the rider's RACE_A/B/C events (read-only) and the
builder's table lets the rider map each one: one goal event, B/C rows, skip.
Hermetic: intervals.icu only through a fake behind icu_calendar_push._http.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import icu_calendar_push as icp  # noqa: E402
from test_icu_push import stub, _apikey  # noqa: E402,F401  (stub is a fixture)
from test_331_surfaces import _extract_js_function  # noqa: E402

DASHBOARD = Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"
TODAY = date(2026, 10, 13)


def _d(n):
    return (TODAY + timedelta(days=n)).isoformat()


class Fake:
    def __init__(self, rows=None, status=200, raw=None):
        self.rows, self.status, self.raw, self.calls = rows or [], status, raw, []

    def __call__(self, method, path, payload=None, timeout=30.0):
        self.calls.append((method, path))
        body = self.raw if self.raw is not None else json.dumps(self.rows).encode()
        return self.status, body


@pytest.fixture()
def env(stub, freeze_clock, monkeypatch):
    freeze_clock(TODAY)
    stub.fake = Fake()
    monkeypatch.setattr(icp, "_http", stub.fake)
    return stub


def _get(env):
    return env.client.get("/api/icu/races").json()


def _ev(day, cat, name="", **kw):
    return dict({"id": 1, "category": cat, "name": name,
                 "start_date_local": f"{day}T00:00:00"}, **kw)


def test_races_are_read_mapped_and_sorted(env):
    _apikey(env)
    env.fake.rows = [
        _ev(_d(90), "RACE_B", "Tour of the Hills", type="Ride", distance=112400),
        _ev(_d(30), "RACE_A", "Gran Fondo", type="Ride", distance=160000.0),
        _ev(_d(40), "WORKOUT", "Z2 Endurance", type="Ride"),
        _ev(_d(50), "NOTE", "Holiday"),
        _ev(_d(-3), "RACE_A", "Last week's race"),
        _ev(_d(60), "RACE_C", "Club crit", type="Ride", distance=True),
        _ev(_d(70), "RACE_C", "Trail run", type="Run", distance=-5),
        _ev("garbled", "RACE_A", "No date"),
        "not an event",
    ]
    r = _get(env)
    assert r == {"races": [
        {"date": _d(30), "name": "Gran Fondo", "priority": "A", "sport": "Ride", "distance_km": 160.0},
        {"date": _d(60), "name": "Club crit", "priority": "C", "sport": "Ride", "distance_km": None},
        {"date": _d(70), "name": "Trail run", "priority": "C", "sport": "Run", "distance_km": None},
        {"date": _d(90), "name": "Tour of the Hills", "priority": "B", "sport": "Ride", "distance_km": 112.4},
    ]}
    (method, path), = env.fake.calls
    assert method == "GET"
    assert f"oldest={_d(0)}" in path and f"newest={_d(365)}" in path


def test_a_race_today_is_included(env):
    _apikey(env)
    env.fake.rows = [_ev(_d(0), "RACE_A", "Today")]
    assert [x["date"] for x in _get(env)["races"]] == [_d(0)]


def test_not_connected_reads_nothing(env):
    assert _get(env) == {"error": "not_connected"}
    assert env.fake.calls == []


def test_oauth_without_calendar_scope_asks_to_reconnect(env, monkeypatch):
    import config
    monkeypatch.setattr(config, "ICU_ACCESS_TOKEN", "tok", raising=False)
    monkeypatch.setattr(config, "ICU_ATHLETE_ID", "i123", raising=False)
    monkeypatch.setattr(type(env.pm), "icu_has_scope", lambda self, s: False)
    assert _get(env) == {"needs_reconnect": True}
    assert env.fake.calls == []


@pytest.mark.parametrize("status,raw,expect", [
    (403, b'{"error":"insufficient scope CALENDAR:READ"}', {"needs_reconnect": True}),
    (401, b'{"error":"token expired"}', {"needs_reconnect": True}),
    (0, b"", {"error": "network"}),
    (500, b"down", {"error": "http_500"}),
    (200, b"<html>", {"error": "bad_response"}),
])
def test_failures_are_plain_answers(env, status, raw, expect):
    _apikey(env)
    env.fake.status, env.fake.raw = status, raw
    assert _get(env) == expect


# ── the builder's table (node) ───────────────────────────────────────────────

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_DOM = r"""
const els = {};
function el(id, value) { els[id] = {id, value: value || '', style: {display: 'none'}, innerHTML: ''}; return els[id]; }
['plan-goal', 'plan-edate', 'plan-ename', 'plan-ekm', 'icu-races-panel'].forEach(i => el(i));
els['plan-goal'].value = 'ftp';
els['plan-ekm'].value = '150';
let bcRows = [];
let picks = [];
const document = {
  getElementById: id => els[id] || null,
  querySelectorAll: sel => sel.includes('icu-race-pick') ? picks
    : sel.includes('plan-bc-row') ? bcRows.map(r => r.node) : [],
};
function addBcRaceRow(ev) {
  const r = {ev: Object.assign({}, ev)};
  r.node = {querySelector: q => q === '.bc-date' ? {value: r.ev.date} : null,
            remove: () => { bcRows = bcRows.filter(x => x !== r); }};
  bcRows.push(r);
}
function readBcRaces() { return bcRows.map(r => ({date: r.ev.date, priority: r.ev.priority})); }
function _isEventGoal(g) { return g === 'event' || g === 'event_preparation'; }
let toggled = 0, synced = 0;
function togglePlanFields() { toggled++; }
function _syncPlanDateToWeeks() { synced++; }
const toasts = [];
function showToast(m) { toasts.push(m); }
"""


def _run(js):
    res = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _fns(*names):
    src = DASHBOARD.read_text(encoding="utf-8")
    return "\n".join(_extract_js_function(src, n) for n in names) + "\nlet _icuRaces = [];\n"


@needs_node
def test_defaults_earliest_a_is_the_goal_later_a_is_b_other_sports_skip():
    out = _run(_fns("_icuRaceDefaults") + """
console.log(JSON.stringify(_icuRaceDefaults([
  {priority: 'A', sport: 'Ride'}, {priority: 'A', sport: 'VirtualRide'},
  {priority: 'C', sport: ''}, {priority: 'A', sport: 'Run'}, {priority: 'B', sport: 'GravelRide'}])));""")
    assert out == ["A", "B", "C", "skip", "B"]


@needs_node
def test_picking_a_second_goal_demotes_the_first():
    out = _run(_fns("icuRacePickChanged") + """
const a = {value: 'A'}, b = {value: 'B'}, c = {value: 'C'};
const document = {querySelectorAll: () => [a, b, c]};
b.value = 'A'; icuRacePickChanged(b);
console.log(JSON.stringify([a.value, b.value, c.value]));""")
    assert out == ["B", "A", "C"]


@needs_node
def test_apply_fills_the_goal_and_adds_b_c_rows_once():
    out = _run(_DOM + _fns("icuRacesApply") + """
_icuRaces = [
  {date: '2026-11-10', name: 'Early C', priority: 'C', distance_km: null},
  {date: '2026-12-01', name: 'Gran Fondo', priority: 'A', distance_km: 160},
  {date: '2026-11-20', name: 'Hills', priority: 'B', distance_km: 112.4},
  {date: '2027-01-15', name: 'After the goal', priority: 'B', distance_km: null},
  {date: '2026-11-25', name: 'A run', priority: 'C', distance_km: null},
];
picks = [{dataset: {i: '0'}, value: 'C'}, {dataset: {i: '1'}, value: 'A'},
         {dataset: {i: '2'}, value: 'B'}, {dataset: {i: '3'}, value: 'B'},
         {dataset: {i: '4'}, value: 'skip'}];
addBcRaceRow({date: '2026-12-01', priority: 'B', name: 'same day as the goal'});
addBcRaceRow({date: '2026-11-10', priority: 'C', name: 'already there'});
const res = icuRacesApply();
const again = icuRacesApply();
console.log(JSON.stringify({goal: els['plan-goal'].value, edate: els['plan-edate'].value,
  ename: els['plan-ename'].value, ekm: els['plan-ekm'].value, toggled, synced,
  rows: bcRows.map(r => [r.ev.date, r.ev.priority, r.ev.name]),
  added: res.added, later: res.later, again: again.added, toast: toasts[0]}));""")
    assert out["goal"] == "event" and out["toggled"] == 1
    assert (out["edate"], out["ename"], out["ekm"]) == ("2026-12-01", "Gran Fondo", 160)
    assert out["rows"] == [["2026-11-10", "C", "already there"], ["2026-11-20", "B", "Hills"]]
    assert (out["added"], out["later"], out["again"]) == (1, 1, 0)
    assert out["toast"].startswith("Added Gran Fondo on 2026-12-01 as the plan goal and 1 B/C race.")
    assert "1 race is after your goal and not in this plan." in out["toast"]


@needs_node
def test_apply_without_a_goal_keeps_the_goal_type_and_adds_all_b_c():
    out = _run(_DOM + _fns("icuRacesApply") + """
_icuRaces = [{date: '2027-03-01', name: 'Spring B', priority: 'B', distance_km: 90}];
picks = [{dataset: {i: '0'}, value: 'B'}];
icuRacesApply();
console.log(JSON.stringify({goal: els['plan-goal'].value, edate: els['plan-edate'].value,
  rows: bcRows.map(r => [r.ev.date, r.ev.priority, r.ev.event_km])}));""")
    assert out == {"goal": "ftp", "edate": "", "rows": [["2027-03-01", "B", 90]]}


@needs_node
def test_b_c_after_the_goal_already_in_the_builder_are_left_out():
    """Grill L4/L5: no A picked in the table, but the builder already has an
    event goal: races after it have no week. And a goal race without a
    distance clears the old event's distance."""
    out = _run(_DOM + _fns("icuRacesApply") + """
els['plan-goal'].value = 'event';
els['plan-edate'].value = '2026-12-01';
_icuRaces = [{date: '2027-02-10', name: 'Late B', priority: 'B', distance_km: null},
             {date: '2026-11-10', name: 'Early C', priority: 'C', distance_km: null}];
picks = [{dataset: {i: '0'}, value: 'B'}, {dataset: {i: '1'}, value: 'C'}];
const res = icuRacesApply();
_icuRaces = [{date: '2027-01-20', name: 'Hill climb', priority: 'A', distance_km: null}];
picks = [{dataset: {i: '0'}, value: 'A'}];
icuRacesApply();
console.log(JSON.stringify({added: res.added, later: res.later, ekm: els['plan-ekm'].value,
  rows: bcRows.map(r => r.ev.date)}));""")
    assert (out["added"], out["later"]) == (1, 1)
    assert out["rows"] == ["2026-11-10"] and out["ekm"] == ""
