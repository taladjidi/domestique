"""Contract 3.4 (file side): the library cache writes never follow a symlink
planted at their temp path.

Both caches were written through a FIXED temp name next to the cache
(`.library_index.json.tmp`, `.workout_facts.json.tmp`) opened with a plain
open(). A rider may point the workout directory at a folder another local
user can write (the setup guard allowed /tmp); a symlink planted at that
name made the app write the cache THROUGH it, into any file the app's user
can write (audit 2026-09-23, rv-http #5: ~/.bashrc stand-in overwritten).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import training_planner as tp
import workout_facts

VICTIM_TEXT = "ORIGINAL VICTIM CONTENT\n"


@pytest.fixture
def planted(tmp_path: Path):
    lib = tmp_path / "shared-lib"
    lib.mkdir()
    victim = tmp_path / "victim_bashrc"
    victim.write_text(VICTIM_TEXT)
    for name in (tp._LIBRARY_INDEX_FILENAME, workout_facts.FACTS_FILENAME):
        (lib / (name + ".tmp")).symlink_to(victim)
    return lib, victim


def _no_stray_temps(lib: Path) -> list[str]:
    return sorted(p.name for p in lib.iterdir()
                  if p.name.endswith(".tmp") and not p.is_symlink())


def test_library_index_write_does_not_follow_a_planted_symlink(planted, monkeypatch):
    lib, victim = planted
    monkeypatch.setattr(tp, "WORKOUT_DIR", lib)
    tp._write_library_index([{"File": "a.zwo"}], 1, 1.0)
    assert victim.read_text() == VICTIM_TEXT
    written = json.loads((lib / tp._LIBRARY_INDEX_FILENAME).read_text())
    assert written["rows"] == [{"File": "a.zwo"}]
    assert not (lib / tp._LIBRARY_INDEX_FILENAME).is_symlink()
    assert _no_stray_temps(lib) == []


def test_facts_cache_write_does_not_follow_a_planted_symlink(planted):
    lib, victim = planted
    workout_facts._write_cache_file(lib, {"a.zwo": {"sha1": "x"}})
    assert victim.read_text() == VICTIM_TEXT
    path = lib / workout_facts.FACTS_FILENAME
    assert path.exists() and not path.is_symlink()
    assert "a.zwo" in path.read_text()
    assert _no_stray_temps(lib) == []


def test_a_failed_cache_write_leaves_no_temp_file(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(tp, "WORKOUT_DIR", lib)

    def _raise(*a, **k):
        raise OSError("disk full")
    with monkeypatch.context() as m:
        m.setattr(workout_facts.os, "replace", _raise)
        tp._write_library_index([], 0, 0.0)          # best-effort: swallowed
        workout_facts._write_cache_file(lib, {})     # best-effort: swallowed
    assert sorted(p.name for p in lib.iterdir()) == []


def test_cache_files_are_readable_by_others(tmp_path, monkeypatch):
    """mkstemp creates 0600; a cache written that way ships unreadable to
    any other user of a packaged bundle (a 50 s re-parse on every launch)."""
    import stat
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(tp, "WORKOUT_DIR", lib)
    tp._write_library_index([], 0, 0.0)
    workout_facts._write_cache_file(lib, {})
    for name in (tp._LIBRARY_INDEX_FILENAME, workout_facts.FACTS_FILENAME):
        assert stat.S_IMODE((lib / name).stat().st_mode) == 0o644, name
