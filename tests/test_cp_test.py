"""3.13.0 — the Critical Power test as a fourth FTP-test protocol.

Three maximal efforts of 12, 7 and 3 min, 30 min apart (Karsten et al. 2015).
CP from P = W'/t + CP over the three; FTP = CP; W' for information only.
Correct or silent: no three real efforts, no number.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import fitness_estimation as fe
from test_icu_push import stub  # noqa: F401  (fixture)

DASHBOARD = Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"
FILE = "ftp_test_cp_12-7-3min_100pct_110min.zwo"


def _ride(p12, p7, p3, easy=130, rest=100):
    return ([easy] * 1200 + [p12] * 720 + [rest] * 1800 + [p7] * 420
            + [rest] * 1800 + [p3] * 180 + [easy] * 480)


def _cp_ride(cp=250.0, wp=20000.0):
    return _ride(*(round(cp + wp / t) for t in (720, 420, 180)))


def test_cp_and_w_prime_come_from_the_three_efforts():
    s = fe.cp_test_ftp(_cp_ride())
    assert s["value"] == s["cp_w"] == 250
    assert abs(s["w_prime_j"] - 20000) < 300 and s["fit_r2"] > 0.99
    assert s["efforts_w"] == {"12min": 278, "7min": 298, "3min": 361}
    assert (s["window_start_s"], s["window_end_s"]) == (1200, 1920)


def test_the_test_is_recognised_by_its_file_or_name_and_scored():
    for hint in (FILE, "FTP Test — CP 12/7/3 Critical Power (110min)", "FTP Test CP 110min"):
        out = fe.evaluate_ftp_test(_cp_ride(), filename_hint=hint, prior_ftp=240)
        assert out["ftp_test_type"] == "cp_test", hint
        sug = out["ftp_test_suggestion"]
        assert (sug["ftp"], sug["method"], sug["source"]) == (250, "cp_test", "tested_cp_test")
        assert sug["pct_delta"] == 4.2 and sug["efforts_w"]["3min"] == 361
        assert "w_prime_j" in sug and "cp_w" in sug


def _zwo_shaped_cp_ride(cp=250.0, wp=20000.0, old_ftp=240):
    """The shipped .zwo ridden as written (grill 3.13.0)."""
    p12, p7, p3 = (round(cp + wp / t) for t in (720, 420, 180))
    f = old_ftp
    warm = [round(f * (0.45 + 0.25 * i / 600)) for i in range(600)]
    openers = ([round(f * 0.95)] * 60 + [round(f * 0.5)] * 60) * 3
    return (warm + openers + [round(f * 0.5)] * 240 + [p12] * 720
            + [round(f * 0.4)] * 1800 + [p7] * 420 + [round(f * 0.4)] * 1800
            + [p3] * 180 + [round(f * (0.6 - 0.2 * i / 480)) for i in range(480)])


@pytest.mark.parametrize("hint", [None, "Morning Ride", "Saturday ride"])
def test_a_cp_ride_without_its_name_is_silent_not_a_20min_test(hint):
    """Grill H1: the 20-min calculator read the 12-min effort plus rest as
    its block, ~17% low. A ride that only looks like a CP test is silent."""
    faded = _zwo_shaped_cp_ride()
    faded[-660:-480] = [330] * 180                 # a 3-min effort below max (grill r2 M4)
    for ride in (_cp_ride(), _zwo_shaped_cp_ride(), faded, _ride(278, 298, 320)):
        assert fe.evaluate_ftp_test(ride, filename_hint=hint, prior_ftp=240) is None


def test_an_app_prefixed_name_still_names_the_cp_test():
    out = fe.evaluate_ftp_test(_zwo_shaped_cp_ride(), prior_ftp=240,
                               filename_hint="Zwift - FTP Test — CP 12/7/3 Critical Power (110min) in Watopia")
    assert out["ftp_test_type"] == "cp_test" and out["ftp_test_suggestion"]["ftp"] == 250


def test_a_submaximal_3min_effort_is_silent_not_an_inflated_cp():
    """Grill M1: 278/298/320 W (the 3 min ridden tired) fitted CP 270 W,
    8% high, at R² 0.93."""
    assert fe.cp_test_ftp(_ride(278, 298, 320)) is None
    # Efforts each ~2% off still score, close to the truth.
    s = fe.cp_test_ftp(_ride(278, 304, 361))
    assert s is not None and abs(s["cp_w"] - 250) <= 12


@pytest.mark.parametrize("method,source", [("cp_test", "tested_cp_test"), ("sixty_min", "tested_sixty_min")])
def test_accepting_the_result_updates_ftp(stub, method, source):
    """Grill C1: Update answered 400 "unknown method" for both protocols."""
    r = stub.client.post("/api/profile/update-ftp", json={
        "ftp": 250, "method": method, "source": source, "applied": True})
    assert r.status_code == 200, r.text
    assert r.json()["ftp"] == 250 and r.json()["ftp_source"] == source


@pytest.mark.parametrize("ride,why", [
    (_ride(300, 290, 280), "the 3-min effort is the weakest"),
    ([150] * 6600, "an easy ride, no efforts"),
    ([130] * 1200 + [300] * 720 + [130] * 4680, "only one effort"),
    (_ride(262, 265, 270), "W' far below 5 kJ"),
    ([130] * 1200 + [300] * 720 + [130] * 300 + [340] * 420 + [130] * 300, "too short for three separate efforts"),
])
def test_silent_without_three_real_efforts(ride, why):
    assert fe.cp_test_ftp(ride) is None, why
    assert fe.evaluate_ftp_test(ride, filename_hint=FILE, prior_ftp=240) is None, why


def test_a_blown_12min_start_is_silent_not_a_guess():
    """Three minutes at 400 W opening the 12-min effort lift its mean above
    the 7-min effort: the efforts no longer describe one power curve."""
    ride = _cp_ride()
    ride[1200:1380] = [400] * 180
    assert fe.cp_test_ftp(ride) is None


def test_a_3min_effort_right_after_the_7min_is_not_a_separate_effort():
    ride = _cp_ride()
    ride[5940:6120] = [100] * 180                  # drop the real 3-min effort
    ride[4140:4320] = [361] * 180                  # and put it 1 min after the 7
    assert fe.cp_test_ftp(ride) is None


def test_app_knows_the_family_file_and_provenance():
    import app as app_module
    import profile_manager
    assert app_module._ftp_test_family(FILE) == "cp_test"
    assert app_module._FTP_TEST_PREFERRED_FILE["cp_test"][0] == FILE
    assert (Path(__file__).resolve().parent.parent / "src" / "workouts" / FILE).exists()
    assert profile_manager._FACTS["ftp"].tiers["tested_cp_test"] == 2  # our facts registry names it .tiers (src/facts.py)


def test_the_ftp_test_type_endpoint_serves_the_cp_file():
    import json as _json
    import training_planner as tp
    from fastapi.testclient import TestClient
    import app as app_module
    from test_plan_api import _mk_plan_dict
    import tempfile
    from datetime import date, timedelta
    with tempfile.TemporaryDirectory() as d:
        old = tp.PLAN_DIR
        tp.PLAN_DIR = Path(d)
        try:
            today = date.today()
            monday = today - timedelta(days=today.weekday())
            plan = _mk_plan_dict(monday)
            day = plan["weeks"][0]["sessions"][2]
            day.update(session_type="ftp_test", zwo_file="ftp_test_coggan_3x1min-1min_95pct_59min.zwo")
            (Path(d) / "current_plan.json").write_text(_json.dumps(plan))
            r = TestClient(app_module.app).post("/api/plan/ftp-test-type",
                                                json={"date": day["day"], "test_type": "cp_test"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["zwo_file"] == FILE and body["duration_min"] == 110
            assert body["tss_estimate"] >= 85
        finally:
            tp.PLAN_DIR = old


def test_execution_score_does_not_grade_the_intensity_of_a_cp_test():
    import execution_score as es
    import inspect
    src = inspect.getsource(es)
    assert '"ftp_test_cp" in _zwo_l' in src


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@needs_node
def test_the_ui_maps_the_file_to_the_cp_protocol():
    from test_331_surfaces import _extract_js_function
    js = _extract_js_function(DASHBOARD.read_text(encoding="utf-8"), "_ftpTypeFromFile") + f"""
console.log(JSON.stringify([_ftpTypeFromFile('{FILE}'), _ftpTypeFromFile('ftp_test_cts_3x1min-1min_105pct_54min.zwo')]));"""
    res = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == ["cp_test", None]


def test_a_riders_own_workout_folder_still_finds_the_cp_test(tmp_path, monkeypatch):
    """Tester report (3.13.0): "the workout is not available in the library".
    A rider's own workout folder predates the CP test file; the file is found
    in the built-in library, and nothing is copied or changed."""
    import json as _json
    import training_planner as tp
    import app as app_module
    from fastapi.testclient import TestClient
    from test_plan_api import _mk_plan_dict
    from datetime import date, timedelta
    own = tmp_path / "my_workouts"
    own.mkdir()
    bundled = Path(app_module._BUNDLED_WORKOUT_DIR)
    for f in ("ftp_test_coggan_3x1min-1min_95pct_59min.zwo", "endurance_steady_68pct_120min.zwo"):
        (own / f).write_bytes((bundled / f).read_bytes())
    plans = tmp_path / "plans"
    plans.mkdir()
    # Our line keeps no app.WORKOUT_DIR (contract 5.1): the app reads the
    # planner's through active_workout_dir(), so patching tp's is enough.
    monkeypatch.setattr(tp, "WORKOUT_DIR", own)
    monkeypatch.setattr(tp, "PLAN_DIR", plans)
    today = date.today()
    plan = _mk_plan_dict(today - timedelta(days=today.weekday()))
    day = plan["weeks"][0]["sessions"][2]
    day.update(session_type="ftp_test", zwo_file="ftp_test_coggan_3x1min-1min_95pct_59min.zwo")
    (plans / "current_plan.json").write_text(_json.dumps(plan))
    client = TestClient(app_module.app)
    r = client.post("/api/plan/ftp-test-type", json={"date": day["day"], "test_type": "cp_test"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["zwo_file"] == FILE and body["duration_min"] == 110
    assert body["zwo_name"].startswith("FTP Test — CP 12/7/3")
    assert not (own / FILE).exists()                       # nothing copied
    assert app_module._workout_file(FILE) == (bundled / FILE).resolve()
    assert app_module._workout_file("endurance_steady_68pct_120min.zwo") == (own / "endurance_steady_68pct_120min.zwo").resolve()
    assert client.get(f"/api/workout/{FILE}/download").status_code in (200, 404)
