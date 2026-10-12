"""The served workout has one owner, fitted to the slot (slim/9.1, S1 S2 S4).

The plan keeps an easy slot's length (contract 6.8 / R4a) and the head unit
ends a workout at the file's last block: the owner's week of 10-19 had 664 min
of slots and 614 min of files on the Bryton. download_lib.served_zwo_bytes is
the one place a planned session's workout is made; the calendar push, the ZWO
download and the FIT export build from it.

notes/served-workout-contract.md holds the assertions.
"""
from __future__ import annotations

import ast
import base64
import inspect
import io
import logging
import re
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import download_lib as dl  # noqa: E402
import zwo_parse  # noqa: E402

LIB = REPO / "src" / "workouts"
E160 = "endurance_steady_62pct_160min.zwo"      # 15 warmup | 135 @ 0.62 | 10 cool


def _total_s(data: bytes) -> int:
    return zwo_parse.scan_zwo(dl._NamedBytes(data))["total_sec"]


def _segs(data: bytes):
    import xml.etree.ElementTree as ET
    return [(e.tag, dict(e.attrib)) for e in ET.fromstring(data).find("workout")]


def _lines_changed(a: bytes, b: bytes):
    la, lb = a.decode().splitlines(), b.decode().splitlines()
    return la, lb


# ── S2: fitted to the slot, for easy slots ───────────────────────────────────

@pytest.mark.parametrize("stype", ["recovery", "z2", "long_z2", "endurance"])
def test_an_easy_slot_longer_than_its_file_serves_the_slot(stype):
    raw = (LIB / E160).read_bytes()
    fit = dl.fit_zwo_to_slot(raw, 177, stype)
    assert fit.status == "fitted" and fit.file_min == 160
    assert _total_s(fit.data) == 177 * 60
    before, after = _segs(raw), _segs(fit.data)
    assert [t for t, _ in before] == [t for t, _ in after]
    assert after[1][1]["Duration"] == str(8100 + 17 * 60)
    # every other block is the library's, attribute for attribute
    assert before[0] == after[0] and before[2] == after[2]
    assert {k: v for k, v in before[1][1].items() if k != "Duration"} == \
        {k: v for k, v in after[1][1].items() if k != "Duration"}


def test_an_easy_slot_shorter_than_its_file_shortens_the_steady_block():
    raw = (LIB / E160).read_bytes()
    fit = dl.fit_zwo_to_slot(raw, 150, "z2")
    assert fit.status == "fitted"
    assert _total_s(fit.data) == 150 * 60
    assert _segs(fit.data)[1][1]["Duration"] == str(8100 - 600)


def test_a_file_within_two_minutes_is_served_as_written():
    raw = (LIB / E160).read_bytes()
    for slot in (159, 160, 161):
        fit = dl.fit_zwo_to_slot(raw, slot, "z2")
        assert fit.status == "within" and fit.data is raw


@pytest.mark.parametrize("stype", ["tempo", "sweetspot", "threshold", "vo2max",
                                   "overunder", "sprint", "ftp_test", ""])
def test_a_hard_slot_is_served_exactly_as_today(stype):
    raw = (LIB / E160).read_bytes()
    fit = dl.fit_zwo_to_slot(raw, 177, stype)
    assert fit.status == "verbatim" and fit.data is raw


def test_no_slot_is_a_library_serve():
    raw = (LIB / E160).read_bytes()
    for slot in (None, 0):
        assert dl.fit_zwo_to_slot(raw, slot, "z2").data is raw


LADDER = b"""<workout_file>
    <name>Ladder</name>
    <workout>
        <Warmup Duration="600" PowerLow="0.5" PowerHigh="0.7"/>
        <SteadyState Duration="1500" Power="0.80"/>
        <SteadyState Duration="900" Power="0.70"/>
        <SteadyState Duration="1200" Power="0.50"/>
        <Cooldown Duration="300" PowerLow="0.6" PowerHigh="0.4"/>
    </workout>
</workout_file>
"""


def test_the_longest_z1_z2_block_is_the_one_that_moves():
    """The 0.80 block is longer but Z3: the 0.50 block (Z1) is the longest in
    Z1-Z2 and takes the minutes; the rest are untouched."""
    fit = dl.fit_zwo_to_slot(LADDER, 100, "z2")          # file 75 min
    assert fit.status == "fitted"
    after = _segs(fit.data)
    assert [a["Duration"] for _, a in after] == ["600", "1500", "900", str(1200 + 1500), "300"]


def test_shortening_never_takes_a_block_below_ten_minutes(caplog):
    # 75 min file to a 60-min slot: the 1200 s block would go to 300 s.
    fit = dl.fit_zwo_to_slot(LADDER, 60, "z2")
    assert fit.status == "unfitted" and fit.data == LADDER
    # 75 -> 65: the block goes to 600 s exactly, which is allowed
    fit = dl.fit_zwo_to_slot(LADDER, 65, "z2")
    assert fit.status == "fitted"
    assert _segs(fit.data)[3][1]["Duration"] == "600"


INTERVALS_ONLY = b"""<workout_file>
    <name>Micro</name>
    <workout>
        <Warmup Duration="600" PowerLow="0.5" PowerHigh="0.75"/>
        <IntervalsT Repeat="10" OnDuration="30" OffDuration="60" OnPower="1.10" OffPower="0.68"/>
        <Cooldown Duration="600" PowerLow="0.6" PowerHigh="0.4"/>
    </workout>
</workout_file>
"""


def test_a_file_with_no_steady_easy_block_gets_one_before_its_cooldown():
    fit = dl.fit_zwo_to_slot(INTERVALS_ONLY, 60, "z2")    # file 35 min
    assert fit.status == "fitted"
    after = _segs(fit.data)
    assert [t for t, _ in after] == ["Warmup", "IntervalsT", "SteadyState", "Cooldown"]
    assert after[2][1] == {"Duration": str(25 * 60), "Power": "0.68"}  # its own easy power
    assert _total_s(fit.data) == 60 * 60
    assert _segs(INTERVALS_ONLY)[1] == after[1]


def test_with_no_easy_power_the_inserted_block_is_065():
    data = INTERVALS_ONLY.replace(b'OffPower="0.68"', b'OffPower="0.45"')
    fit = dl.fit_zwo_to_slot(data, 60, "z2")
    assert _segs(fit.data)[2][1]["Power"] == "0.65"


def test_without_a_cooldown_the_block_goes_last():
    data = INTERVALS_ONLY.replace(
        b'        <Cooldown Duration="600" PowerLow="0.6" PowerHigh="0.4"/>\n', b"")
    fit = dl.fit_zwo_to_slot(data, 60, "z2")
    assert fit.status == "fitted"
    assert [t for t, _ in _segs(fit.data)] == ["Warmup", "IntervalsT", "SteadyState"]
    assert _total_s(fit.data) == 60 * 60


def test_a_file_with_no_steady_easy_block_and_a_shorter_slot_is_unfitted():
    fit = dl.fit_zwo_to_slot(INTERVALS_ONLY, 25, "z2")
    assert fit.status == "unfitted" and fit.data == INTERVALS_ONLY


@pytest.mark.parametrize("dur", ["Duration='2400'", 'Duration = "2400"'])
def test_an_edit_that_does_not_read_back_as_the_slot_is_unfitted(dur):
    # The edit is made on the text: an attribute spelled another way is
    # missed, and the file must be served as written, not under a name
    # restated to the slot (evaluator, slim/9.1).
    raw = (b"<workout_file><name>Easy (60min)</name><workout>"
           b"<Warmup Duration=\"600\" PowerLow=\"0.4\" PowerHigh=\"0.6\" />"
           b"<SteadyState " + dur.encode() + b" Power=\"0.62\" />"
           b"<Cooldown Duration=\"600\" PowerLow=\"0.6\" PowerHigh=\"0.4\" />"
           b"</workout></workout_file>")
    assert _total_s(raw) == 3600
    fit = dl.fit_zwo_to_slot(raw, 75, "z2")
    assert fit.status == "unfitted" and fit.data == raw


def test_an_unfitted_serve_is_logged_and_counted(tmp_path, caplog):
    p = tmp_path / "ladder.zwo"
    p.write_bytes(LADDER)
    before = dl.served_unfitted["ladder.zwo"]
    with caplog.at_level(logging.WARNING, logger="domestique.download"):
        out = dl.served_zwo_bytes(p, 60, "z2", None)
    assert out == LADDER
    assert dl.served_unfitted["ladder.zwo"] == before + 1
    assert any("ladder.zwo" in r.getMessage() for r in caplog.records)


def test_the_cap_applies_to_the_fitted_workout(tmp_path, monkeypatch):
    """S1: fitted to the slot, THEN the measured-capacity cap exactly as
    today (same function, same file name)."""
    p = tmp_path / E160
    p.write_bytes((LIB / E160).read_bytes())
    seen = []

    def fake_cap(raw, filename, pm):
        seen.append((raw, filename, pm))
        return raw + b"<!-- capped -->"
    monkeypatch.setattr(dl, "_cap_zwo_bytes", fake_cap)
    pm = object()
    out = dl.served_zwo_bytes(p, 177, "z2", pm, cap=True)
    assert seen == [(dl.fit_zwo_to_slot(p.read_bytes(), 177, "z2").data, E160, pm)]
    assert out.endswith(b"<!-- capped -->")
    seen.clear()
    monkeypatch.setattr(dl, "_capacity_cap_active", lambda pm, force=False: False)
    assert dl.served_zwo_bytes(p, 177, "z2", pm) == \
        dl.fit_zwo_to_slot(p.read_bytes(), 177, "z2").data and seen == []


# ── S4: deterministic ────────────────────────────────────────────────────────

def test_the_same_session_serves_the_same_bytes(tmp_path):
    p = tmp_path / E160
    p.write_bytes((LIB / E160).read_bytes())
    outs = {dl.served_zwo_bytes(p, 177, "z2", None) for _ in range(5)}
    assert len(outs) == 1
    assert dl.fit_zwo_to_slot(next(iter(outs)), 177, "z2").status == "within"


# ── S1: one owner, every surface builds from it ──────────────────────────────

def _src(fn) -> str:
    return textwrap.dedent(inspect.getsource(fn))


def _serving_functions():
    import app
    import icu_calendar_push as icp
    import launcher
    return {
        "icu_calendar_push._build_event": icp._build_event,
        "app.build_fit_workout_bytes": app.build_fit_workout_bytes,
        "app.download_zwo_flat": app.download_zwo_flat,
        "download_lib._zwo_download_response": dl._zwo_download_response,
        "launcher.JsApi._read_zwo_serverside": launcher.JsApi._read_zwo_serverside,
    }


def test_every_serving_path_builds_from_served_zwo_bytes():
    """Static: the push, the planned download, the FIT export (and the
    desktop fallbacks) reach the workout's bytes through served_zwo_bytes and
    never read, cap or transform them on their own."""
    forbidden = re.compile(r"\.read_bytes\(|\.read_text\(|\bopen\(|_cap_zwo_bytes\(")
    for name, fn in _serving_functions().items():
        src = _src(fn)
        assert "served_zwo_bytes(" in src or "build_fit_workout_bytes(" in src \
            or "_zwo_download_response(" in src, f"{name} does not build from the served workout"
        bad = forbidden.findall(src)
        assert not bad, f"{name} reads/caps workout bytes another way: {bad}"


def test_the_cap_is_applied_in_one_place():
    calls = []
    for f in sorted((REPO / "src").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call)
                            and getattr(sub.func, "id", getattr(sub.func, "attr", "")) == "_cap_zwo_bytes"):
                        calls.append((f.name, node.name))
    # the legacy library route /api/workout/{filename}/download goes through
    # served_zwo_bytes too, so the cap has exactly one caller
    assert sorted(set(calls)) == [("download_lib.py", "served_zwo_bytes")], calls


# ── S1 behaviour: push, download, FIT export serve the fitted workout ───────

from test_icu_push import (  # noqa: E402,F401  (fixtures + plan helpers)
    stub, fake, _write_plan, _apikey, _sess, _eid)


@pytest.fixture()
def easy(stub):
    (stub.workouts / E160).write_bytes((LIB / E160).read_bytes())
    return stub


def test_the_push_serves_the_fitted_workout(easy, fake):
    import icu_calendar_push as icp
    _apikey(easy)
    from test_icu_push import D1
    _write_plan([_sess(D1, zwo=E160, stype="z2", duration_min=177)])
    r = icp.reconcile()
    assert r["pushed"] == 1
    ev = next(c for c in fake.calls if c[0] == "POST")[2][0]
    body = base64.b64decode(ev["file_contents_base64"])
    assert _total_s(body) == 177 * 60
    assert body == dl.served_zwo_bytes(easy.workouts / E160, 177, "z2", easy.pm)
    # S4: a second reconcile after the push changes nothing
    r2 = icp.reconcile()
    assert (r2["pushed"], r2["updated"], r2["deleted"]) == (0, 0, 0)


def test_a_library_push_is_served_as_written(easy, fake):
    import icu_calendar_push as icp
    from test_icu_push import D1
    s = {"zwo_file": E160, "day": D1, "session_type": "z2", "zwo_name": "x"}
    ev, reason = icp._build_event(s, "domestique-manual:x", easy.pm, {},
                                  easy.workouts, False, True)
    assert reason is None
    assert base64.b64decode(ev["file_contents_base64"]) == (LIB / E160).read_bytes()


def _fit_total_s(data: bytes) -> float:
    fitparse = pytest.importorskip("fitparse")
    return sum((m.get_value("duration_time") or 0)
               for m in fitparse.FitFile(io.BytesIO(data)).get_messages("workout_step"))


def test_the_hr_push_fit_is_the_fitted_workout(easy, fake):
    import icu_calendar_push as icp
    from test_icu_push import D1
    easy.pm._athlete.update({"target_mode": "hr", "lthr": 160, "max_hr": 185})
    s = _sess(D1, zwo=E160, stype="z2", duration_min=177)
    ev, reason = icp._build_event(s, _eid(easy, D1), easy.pm, {}, easy.workouts, True, True)
    assert reason is None
    assert round(_fit_total_s(base64.b64decode(ev["file_contents_base64"]))) == 177 * 60


def test_the_planned_zwo_download_serves_the_fitted_workout(easy):
    r = easy.client.get(f"/api/download/zwo/{E160}?slot_min=177&session_type=z2")
    assert r.status_code == 200
    assert _total_s(r.content) == 177 * 60
    # a library download (no slot) is the file as written
    r = easy.client.get(f"/api/download/zwo/{E160}")
    assert r.content == (LIB / E160).read_bytes()


def test_the_fit_export_serves_the_fitted_workout(easy):
    for view in ("power", "hr"):
        if view == "hr":
            easy.pm._athlete.update({"target_mode": "hr", "lthr": 160, "max_hr": 185})
        r = easy.client.get(
            f"/api/export/fit-workout?session_type=z2&duration_min=177&name=x"
            f"&zwo_file={E160}&view={view}&slot_min=177")
        assert r.status_code == 200, r.text
        assert round(_fit_total_s(r.content)) == 177 * 60, view
    r = easy.client.get(
        f"/api/export/fit-workout?session_type=z2&duration_min=160&name=x&zwo_file={E160}")
    assert round(_fit_total_s(r.content)) == 160 * 60


def test_the_desktop_fallbacks_serve_the_fitted_workout(easy):
    import launcher
    api = launcher.JsApi.__new__(launcher.JsApi)
    assert _total_s(api._read_zwo_serverside(E160, 177, "z2")) == 177 * 60
    assert api._read_zwo_serverside(E160) == (LIB / E160).read_bytes()
    fit = api._build_fit_serverside("z2", 177, "x", E160, slot_min=177)
    assert round(_fit_total_s(fit)) == 177 * 60


def test_the_dashboard_passes_the_slot_for_a_planned_session():
    """Every planned-session download button hands the server the slot; the
    library buttons do not."""
    html = (REPO / "src" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    planned_zwo = re.findall(r"downloadZwoFile\('\$\{escJs\((adj\.zwo_file|session\.zwo_file|String\(c\.zwo_file \|\| ''\))\)\}'([^)]*)\)", html)
    assert len(planned_zwo) == 3 and all("duration_min" in rest for _, rest in planned_zwo), planned_zwo
    planned_fit = [m for m in re.findall(r"downloadFIT\(([^\n]*?)\)\">", html)
                   if "adj." in m or "session." in m or "c.session_type" in m]
    assert len(planned_fit) == 4 and all(
        re.search(r"false, \$\{Number\((adj|session|c)\.duration_min", m) for m in planned_fit), planned_fit
    assert "if (slotMin) url += `?slot_min=" in html
    assert "if (slotMin) url += `&slot_min=" in html


# ── S3: every surface says what is served ────────────────────────────────────

def _texts(data: bytes):
    import xml.etree.ElementTree as ET
    r = ET.fromstring(data)
    return r.findtext("name"), r.findtext("description")


def test_the_fitted_zwo_states_the_slots_minutes():
    raw = (LIB / E160).read_bytes()
    name, desc = _texts(dl.fit_zwo_to_slot(raw, 177, "z2").data)
    assert name == "Endurance Steady (177min)"
    assert desc == ("Warmup: 15min from 45% to 65% FTP | Steady: 152min @ 62% FTP"
                    " | Cooldown: 10min from 60% to 40% FTP")
    # served as written: the file's own words
    assert _texts(dl.fit_zwo_to_slot(raw, 161, "z2").data) == _texts(raw)
    assert _texts(dl.fit_zwo_to_slot(raw, 177, "tempo").data) == _texts(raw)


def test_an_inserted_block_is_in_the_description():
    data = INTERVALS_ONLY.replace(
        b"<name>Micro</name>",
        b"<name>Micro (35min)</name>\n    <description>Warmup: 10min | "
        b"Micro: 10 x 30s | Cooldown: 10min</description>")
    name, desc = _texts(dl.fit_zwo_to_slot(data, 60, "z2").data)
    assert name == "Micro (60min)"
    assert desc == "Warmup: 10min | Micro: 10 x 30s | Steady: 25min @ 68% FTP | Cooldown: 10min"


def test_the_pushed_event_name_states_the_slots_minutes(easy):
    import icu_calendar_push as icp
    from test_icu_push import D1
    cls = {E160: {"display_name": "Endurance 160min \u2014 Z2"}}
    for slot, want in ((177, "Endurance 177min \u2014 Z2"),
                       (161, "Endurance 160min \u2014 Z2")):
        ev, _ = icp._build_event(_sess(D1, zwo=E160, stype="z2", duration_min=slot),
                                 _eid(easy, D1), easy.pm, cls, easy.workouts, False, True)
        assert ev["name"] == want
    ev, _ = icp._build_event(_sess(D1, zwo=E160, stype="threshold", duration_min=177),
                             _eid(easy, D1), easy.pm, cls, easy.workouts, False, True)
    assert ev["name"] == "Endurance 160min \u2014 Z2"


def test_the_fit_states_the_slots_minutes(easy):
    fitparse = pytest.importorskip("fitparse")
    data = __import__("app").build_fit_workout_bytes(
        "z2", 177, "endurance_steady_62pct_160min", E160, view="power", slot_min=177)
    names = [m.get_value("wkt_name") for m in
             fitparse.FitFile(io.BytesIO(data)).get_messages("workout")]
    assert names == ["endurance_steady_62pct_177min"]


Z2_119 = "z2_steady_61pct_119min.zwo"


@pytest.mark.parametrize("fname,slot", [(E160, 177), (Z2_119, 150)])
def test_the_stored_session_says_what_is_served(fname, slot, monkeypatch):
    """6.8 / S3: the session's length is the served workout's, and its TSS
    (the file's intensity over the slot, _restamp) is the served workout's
    load within _restamp's own tolerance. Served unfitted, the 160-min file
    in a 177-min slot carried 10.6 % less than the session claimed."""
    import types
    import training_planner as tp
    monkeypatch.setattr(tp, "WORKOUT_DIR", LIB)
    row = next(r for r in tp.load_workout_library() if r["File"] == fname)
    s = types.SimpleNamespace(session_type="z2", duration_min=slot, tss_estimate=0,
                              zwo_file=fname, description=f"Z2 {slot}min")
    tp._restamp(s, {fname: row})
    served = dl.served_zwo_bytes(LIB / fname, s.duration_min, "z2", None)
    facts = zwo_parse.scan_zwo(dl._NamedBytes(served))
    assert facts["total_sec"] == s.duration_min * 60
    load = facts["tss"]
    assert abs(s.tss_estimate - load) <= (tp._MATCH_BUDGET_TOLERANCE - 1.0) * load + 0.5, \
        (s.tss_estimate, load)


# ── S3: the planned-session modal shows the served workout ───────────────────

def test_the_workout_payload_of_a_planned_session_is_the_served_workout(easy):
    """/api/workout/all/<file> feeds the modal's chart and minutes: with the
    session's slot it is the fitted workout, without one the file."""
    r = easy.client.get(f"/api/workout/all/{E160}?slot_min=177&session_type=z2&view=power")
    assert r.status_code == 200
    d = r.json()
    assert d["total_seconds"] == 177 * 60 and d["name"] == "Endurance Steady (177min)"
    assert sum(s.get("duration", 0) for s in d["segments"]) == 177 * 60
    d = easy.client.get(f"/api/workout/all/{E160}?view=power").json()
    assert d["total_seconds"] == 160 * 60


def test_the_modal_headline_and_chart_are_the_served_workout():
    import json
    import shutil
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    import test_341_m3 as m3
    src = m3.DASHBOARD.read_text(encoding="utf-8")
    session = {"day": "__TODAY__", "day_name": "Monday", "session_type": "z2",
               "content_class": "", "duration_min": 177, "tss_estimate": 110,
               "zwo_file": E160, "zwo_name": "Endurance Steady (160min)",
               "display_name": "", "zwo_duration_min": 160,
               "description": "Z2 177min", "zone_dist": None, "status": "pending"}
    harness = m3._MODAL_STUBS + m3._fns(
        src, "_sessTypeLabel", "_adjPlannedNowHtml", "_effectiveTodaySession",
        "_dayModalModel", "_todayDayModalModel", "_calTodayStr",
        "_dayModalTitle", "_adjBannerHtml", "_adjOriginalBlockHtml",
        "dayModalTssStat", "openDayWorkout"
    ) + """
const SESSION = %s; SESSION.day = new Date().toLocaleDateString('en-CA');
(async () => {
  global.window = {_weekPlanSessions: [SESSION], _targetMode: 'power'};
  const asked = [];
  global.fetch = async (url) => {
    if (url === '/api/settings') return {ok: true, json: async () => ({ftp: 250, lthr: 170})};
    if (String(url).startsWith('/api/workout/all/')) {
      asked.push(String(url));
      const served = String(url).includes('slot_min=177') && String(url).includes('session_type=z2');
      const s = served ? 10620 : 9600;
      return {ok: true, json: async () => ({segments: [{type: 'SteadyState', duration: s, power: 155}],
                                            ftp: 250, total_seconds: s})};
    }
    if (String(url).startsWith('/api/')) return {ok: false, json: async () => ({})};
    throw new Error('unexpected fetch ' + url);
  };
  await openDayWorkout(0);
  const h2 = (modalHtml.match(/<h2>[\\s\\S]*?<\\/h2>/) || [''])[0];
  if (!h2.includes('(177min)')) throw new Error('headline is not the served length: ' + h2 + ' ' + asked);
  if (!asked.some(u => u.includes('slot_min=177'))) throw new Error('chart not asked for the served workout: ' + asked);
  if (!modalHtml.includes('data-real="1"')) throw new Error('real segment chart missing');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
""" % json.dumps(session)
    m3._run_node(harness)
