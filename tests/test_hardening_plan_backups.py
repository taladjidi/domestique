"""Contract 3.3: a failed plan write does not rotate the plan backups.

The plan write (training_planner.atomic_write_plan then, plan_store's
private writer now) used to rotate live -> .bak -> ... -> .bak7 BEFORE it wrote
the new file. Every write that then failed (disk full, a directory squatting
on the temp path, a serialisation error) still aged the chain by one, so
seven failures in a row left eight copies of the same generation and pushed
every older snapshot off the end (audit 2026-09-23, rv-struct #4).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import plan_store as ps


def _write(tmp_path: Path, plan: dict) -> None:
    ps.bind("bk", tmp_path)
    ps.update("bk", lambda _p: plan, reason="test")


def _chain(p: Path) -> list:
    out = [json.loads(p.read_text())["v"]]
    for n in range(1, ps.BACKUP_DEPTH + 1):
        b = p.with_suffix(p.suffix + (".bak" if n == 1 else f".bak{n}"))
        out.append(json.loads(b.read_text())["v"] if b.exists() else None)
    return out


def test_failed_writes_leave_the_backup_chain_alone(tmp_path: Path) -> None:
    p = tmp_path / "current_plan.json"
    for i in range(1, 9):
        _write(tmp_path, {"v": i})
    before = _chain(p)
    assert before == [8, 7, 6, 5, 4, 3, 2, 1]

    (tmp_path / "current_plan.tmp").mkdir()  # every write now fails at open(tmp)
    fails = 0
    for i in range(7):
        try:
            _write(tmp_path, {"v": 100 + i})
        except OSError:
            fails += 1
    assert fails == 7
    assert _chain(p) == before


def test_serialisation_failure_leaves_chain_and_no_temp(tmp_path: Path) -> None:
    p = tmp_path / "current_plan.json"
    _write(tmp_path, {"v": 1})
    _write(tmp_path, {"v": 2})
    before = _chain(p)

    with patch("plan_store.json.dump", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            _write(tmp_path, {"v": 3})

    assert _chain(p) == before
    assert not (tmp_path / "current_plan.tmp").exists()


def test_a_good_write_still_rotates(tmp_path: Path) -> None:
    p = tmp_path / "current_plan.json"
    for i in (1, 2, 3):
        _write(tmp_path, {"v": i})
    assert _chain(p)[:4] == [3, 2, 1, None]


def test_a_failed_rename_leaves_the_chain_and_no_temp(tmp_path: Path, monkeypatch) -> None:
    """The last failure point: the rename itself (EBUSY on a locked file)."""
    p = tmp_path / "current_plan.json"
    for i in range(1, 9):
        _write(tmp_path, {"v": i})
    before = _chain(p)
    real_replace = Path.replace

    def busy(self, target):
        if self.name == "current_plan.tmp":
            raise OSError(16, "Device or resource busy")
        return real_replace(self, target)
    monkeypatch.setattr(Path, "replace", busy)
    for i in range(7):
        with pytest.raises(OSError):
            _write(tmp_path, {"v": 100 + i})
    monkeypatch.undo()
    assert _chain(p) == before
    assert sorted(x.name for x in tmp_path.iterdir()           # the store's lock file stays
                  if not x.name.startswith("current_plan.json")) == ["current_plan.lock"]
    assert not (tmp_path / "current_plan.json.bak.new").exists()
