"""3.13.1 — the built-in library is always the library; a rider's own folder
ADDS workouts to it (it used to replace it, and then lacked every workout
shipped later: "no cp_test workout in library", tester report on 3.13.0).

A folder that is itself a Domestique built-in library (the setup wizard
saved the install's own path, so many profiles point at an old install) is
never an extra folder.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import training_planner as tp
from test_icu_push import stub  # noqa: F401  (fixture: sandboxed HOME + TestClient)

BUNDLED = tp._BUNDLED_WORKOUT_DIR
CP = "ftp_test_cp_12-7-3min_100pct_110min.zwo"
MINE = """<workout_file><name>My Hill Repeats</name><workout>
<Warmup Duration="600" PowerLow="0.5" PowerHigh="0.7"/>
<IntervalsT Repeat="5" OnDuration="180" OffDuration="180" OnPower="1.05" OffPower="0.5"/>
<Cooldown Duration="300" PowerLow="0.6" PowerHigh="0.4"/></workout></workout_file>"""


def _own_folder(root: Path) -> Path:
    own = root / "My Workouts"
    own.mkdir()
    (own / "my_hill_repeats.zwo").write_text(MINE, encoding="utf-8")
    # a file the built-in library also has: the built-in one wins
    (own / "endurance_steady_68pct_120min.zwo").write_text(MINE, encoding="utf-8")
    return own


def test_the_library_is_built_in_plus_the_extra_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "WORKOUT_DIR", BUNDLED)
    monkeypatch.setattr(tp, "EXTRA_WORKOUT_DIR", None)
    base = tp.load_workout_library()
    assert any(w["File"] == CP for w in base)
    monkeypatch.setattr(tp, "EXTRA_WORKOUT_DIR", _own_folder(tmp_path))
    lib = tp.load_workout_library()
    files = [w["File"] for w in lib]
    assert len(lib) == len(base) + 1 and "my_hill_repeats.zwo" in files
    assert files.count("endurance_steady_68pct_120min.zwo") == 1
    steady = next(w for w in lib if w["File"] == "endurance_steady_68pct_120min.zwo")
    assert steady["Name"] != "My Hill Repeats"            # the built-in row
    assert CP in files                                     # always there


def test_only_the_running_built_in_library_is_refused_as_extra(tmp_path):
    assert tp.extra_workout_dir_for(BUNDLED) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert tp.extra_workout_dir_for(empty) is None
    assert tp.extra_workout_dir_for(_own_folder(tmp_path)) is not None


def _library_copy(root: Path) -> Path:
    """A copy of an older install's library (its manifest lists its files),
    with one workout the rider added and one old name the library retired."""
    old = root / "Domestique-3.12.2" / "_internal" / "workouts"
    old.mkdir(parents=True)
    (old / "endurance_steady_68pct_120min.zwo").write_text(MINE, encoding="utf-8")
    (old / "z2_old_name_60min.zwo").write_text(MINE, encoding="utf-8")
    (old / "my_hill_repeats.zwo").write_text(MINE, encoding="utf-8")
    (old / ".overhaul_manifest.json").write_text(json.dumps({"entries": [
        {"old_filename": "z2_old_name_60min.zwo", "new_filename": "z2_old_name_60min.zwo"},
        {"old_filename": "x.zwo", "new_filename": "endurance_steady_68pct_120min.zwo"}]}))
    return old


def test_a_library_copy_adds_only_the_riders_own_workouts(tmp_path, monkeypatch):
    """Grill 3.13.1 M1: a copy that carries the manifest was ignored, so the
    rider's own additions vanished; old retired names must not come back."""
    old = _library_copy(tmp_path)
    assert tp.extra_workout_dir_for(old) == old
    monkeypatch.setattr(tp, "WORKOUT_DIR", BUNDLED)
    monkeypatch.setattr(tp, "EXTRA_WORKOUT_DIR", old)
    files = [w["File"] for w in tp.load_workout_library()]
    assert "my_hill_repeats.zwo" in files and "z2_old_name_60min.zwo" not in files
    assert tp.extra_only_names(old, {p.name for p in BUNDLED.glob("*.zwo")}) == {"my_hill_repeats.zwo"}


def test_a_riders_own_workout_can_be_planned(tmp_path, monkeypatch):
    """Grill 3.13.1 H1: the facts gate looked the file up in the built-in
    folder only, so an extra-folder workout was never scheduled."""
    import shutil
    extra = tmp_path / "mine"
    extra.mkdir()
    src = next(BUNDLED.glob("endurance_steady_*.zwo"))
    shutil.copy2(src, extra / "my_own_endurance_ride.zwo")
    monkeypatch.setattr(tp, "WORKOUT_DIR", BUNDLED)
    monkeypatch.setattr(tp, "EXTRA_WORKOUT_DIR", extra)
    rows = tp.load_workout_library()
    mine = next(r for r in rows if r.get("File") == "my_own_endurance_ride.zwo")
    twin = next(r for r in rows if r.get("File") == src.name)
    st = tp._session_type_from_row(twin)
    assert tp.file_admissible(tp._session_type_from_row(mine), mine)
    assert tp.file_admissible(st, twin)


def test_a_saved_folder_resolves_as_extra_and_the_built_in_stays(stub):
    import app as app_module
    own = _own_folder(stub.home)
    (stub.pm.active_dir / "user_paths.json").write_text(json.dumps({"workout_dir": str(own)}))
    stub.pm.apply_training_dirs()
    app_module._apply_profile_paths()
    assert tp.WORKOUT_DIR.resolve() == BUNDLED.resolve()
    # Our line: the planner owns the extra folder (contract 5.1); the app
    # reads tp.EXTRA_WORKOUT_DIR late and keeps no copy to drift.
    assert tp.EXTRA_WORKOUT_DIR == own and not hasattr(app_module, "EXTRA_WORKOUT_DIR")
    assert app_module._workout_file("my_hill_repeats.zwo") == (own / "my_hill_repeats.zwo").resolve()
    assert app_module._workout_file(CP) == (BUNDLED / CP).resolve()
    steady = app_module._workout_file("endurance_steady_68pct_120min.zwo")
    assert steady.parent == BUNDLED.resolve()


def test_an_old_install_path_saved_by_setup_adds_nothing_old(stub):
    import app as app_module
    old = _library_copy(stub.home)
    (stub.pm.active_dir / "user_paths.json").write_text(json.dumps({"workout_dir": str(old)}))
    stub.pm.apply_training_dirs()
    app_module._apply_profile_paths()
    assert tp.WORKOUT_DIR.resolve() == BUNDLED.resolve()
    names = [p.name for p in app_module._library_zwo_paths()]
    assert "z2_old_name_60min.zwo" not in names and "my_hill_repeats.zwo" in names
    assert names.count("endurance_steady_68pct_120min.zwo") == 1


def test_library_tags_follow_the_extra_folder(stub):
    """Grill 3.13.1 L1: the tags cache keyed on max(mtime) only."""
    import app as app_module
    own = stub.home / "Tagged"
    own.mkdir()
    (own / "tagged.zwo").write_text(MINE.replace("<workout>", '<tags><tag name="rider_only_tag"/></tags><workout>', 1))
    c = stub.client
    assert "rider_only_tag" not in json.dumps(c.get("/api/workouts/tags").json())
    assert c.post("/api/settings/workout-folders", json={"extra_dir": str(own)}).status_code == 200
    assert "rider_only_tag" in json.dumps(c.get("/api/workouts/tags").json())
    assert c.post("/api/settings/workout-folders", json={"extra_dir": ""}).status_code == 200
    assert "rider_only_tag" not in json.dumps(c.get("/api/workouts/tags").json())


def test_a_quoted_path_and_a_subfolder_hint(stub):
    own = _own_folder(stub.home)
    r = stub.client.post("/api/settings/workout-folders", json={"extra_dir": f'"{own}"'})
    assert r.status_code == 200 and r.json()["extra"]["path"] == str(own)
    parent = stub.home / "Zwift" / "Workouts"
    (parent / "12345").mkdir(parents=True)
    (parent / "12345" / "a.zwo").write_text(MINE)
    r = stub.client.post("/api/settings/workout-folders", json={"extra_dir": str(parent)})
    assert r.status_code == 400 and "folder inside it" in r.json()["error"]


def test_settings_set_show_and_clear_the_extra_folder(stub):
    c = stub.client
    st = c.get("/api/settings/workout-folders").json()
    assert st["extra"] is None and st["builtin"]["count"] >= 4000
    own = _own_folder(stub.home)
    r = c.post("/api/settings/workout-folders", json={"extra_dir": str(own)})
    assert r.status_code == 200, r.text
    assert r.json()["extra"] == {"path": str(own), "files": 2, "added": 1}
    assert json.loads((stub.pm.active_dir / "user_paths.json").read_text())["workout_dir"] == str(own)
    assert tp.EXTRA_WORKOUT_DIR == own
    assert any(w["File"] == "my_hill_repeats.zwo" for w in tp.load_workout_library())
    r = c.post("/api/settings/workout-folders", json={"extra_dir": ""})
    assert r.status_code == 200 and r.json()["extra"] is None
    assert "workout_dir" not in json.loads((stub.pm.active_dir / "user_paths.json").read_text())
    assert tp.EXTRA_WORKOUT_DIR is None


@pytest.mark.parametrize("why", ["no .zwo", "outside home", "the built-in library"])
def test_settings_refuse_a_folder_that_cannot_be_an_extra_library(stub, why):
    p = {"no .zwo": stub.home / "nothing_here", "outside home": Path("/etc"),
         "the built-in library": BUNDLED}[why]
    if why == "no .zwo":
        p.mkdir()
    r = stub.client.post("/api/settings/workout-folders", json={"extra_dir": str(p)})
    assert r.status_code == 400 and r.json()["error"], why
    assert tp.EXTRA_WORKOUT_DIR is None
