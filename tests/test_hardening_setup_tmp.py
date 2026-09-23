"""Contract 3.4 (app side): `/tmp` is not an allowed base for a user-chosen
workout directory.

Any local user can write under /tmp, so a library there is a library someone
else controls; the planner's caches are written next to it (audit 2026-09-23,
rv-http #5). The Windows TEMP/TMP arm was the same hole: the bundled library's
own directories are allowed on their own, which is what a onefile build
unpacked in %TEMP% needed.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

import app as app_module
import training_planner as tp
from profile_manager import ProfileManager


def test_tmp_is_not_an_allowed_setup_base():
    assert not app_module._setup_path_allowed(Path("/tmp/x"))
    assert not app_module._setup_path_allowed(Path("/tmp"))


def test_windows_temp_is_not_an_allowed_setup_base(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.sys, "platform", "win32")
    temp = Path(tempfile.mkdtemp(prefix="dq-wintemp-", dir="/tmp"))
    try:
        monkeypatch.setenv("TEMP", str(temp))
        monkeypatch.setenv("TMP", str(temp))
        assert not app_module._setup_path_allowed(temp / "lib")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def test_setup_save_refuses_a_workout_dir_under_tmp():
    from fastapi.testclient import TestClient
    lib = Path(tempfile.mkdtemp(prefix="dq-lib-", dir="/tmp"))
    try:
        src = sorted((Path(tp.__file__).parent / "workouts").glob("z2_*.zwo"))[0]
        shutil.copy(src, lib / "planted.zwo")
        pm = ProfileManager.get()
        paths_file = pm.active_dir / "user_paths.json"
        before = paths_file.read_bytes() if paths_file.exists() else None
        r = TestClient(app_module.app).post("/api/setup/save",
                                            json={"workout_dir": str(lib)})
        assert r.status_code == 400, r.text
        assert "/tmp" not in r.json().get("detail", "")
        after = paths_file.read_bytes() if paths_file.exists() else None
        assert after == before
        assert Path(tp.active_workout_dir()) != lib
    finally:
        shutil.rmtree(lib, ignore_errors=True)
