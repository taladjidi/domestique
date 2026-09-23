"""tools/gate.sh fails loudly when the full run produced no pytest summary.

A full run that never reaches pytest's summary line -- uv unable to resolve
its packages (a network namespace without UV_OFFLINE=1), a bad interpreter,
the 55-minute timeout -- has no FAILED lines either, so the gate printed
"real: 0" and exited 0: green. The control here is a worktree whose
interpreter cannot start.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "tools" / "gate.sh"


def _fake_tree(tmp_path: Path, python_target: "Path | None") -> Path:
    wt = tmp_path / "wt"
    (wt / "tests").mkdir(parents=True)
    (wt / "tests" / "known-failures-clean-main.txt").write_text("")
    (wt / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (wt / ".venv" / "bin").mkdir(parents=True)
    py = wt / ".venv" / "bin" / "python"
    if python_target is None:
        py.write_text("#!/bin/sh\necho 'bogus interpreter' >&2\nexit 1\n")
        py.chmod(0o755)
    else:
        py.symlink_to(python_target)
    return wt


def _run_gate(wt: Path, out: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, GATE_OUT=str(out))
    # conftest points HOME at a sandbox; uv's package cache is the real one
    # (offline, the gate can only use what is already cached).
    real_home = os.environ.get("DOMESTIQUE_REAL_HOME")
    if real_home and "UV_CACHE_DIR" not in env:
        env["UV_CACHE_DIR"] = str(Path(real_home) / ".cache" / "uv")
    return subprocess.run(["bash", str(GATE), str(wt), "ctl"], env=env, cwd=wt,
                          capture_output=True, text=True, timeout=600)


@pytest.mark.skipif(shutil.which("uv") is None, reason="the gate runs pytest through uv")
def test_no_summary_is_a_loud_failure(tmp_path):
    r = _run_gate(_fake_tree(tmp_path, None), tmp_path / "out")
    assert r.returncode != 0, r.stdout
    assert "NO PYTEST SUMMARY" in r.stdout
    assert "real: 0" not in r.stdout


@pytest.mark.skipif(shutil.which("uv") is None, reason="the gate runs pytest through uv")
def test_a_completed_run_still_reports_its_summary(tmp_path):
    """Positive control: the same tree with a working interpreter passes."""
    venv_py = REPO / ".venv" / "bin" / "python"
    if not venv_py.exists():
        pytest.skip("no .venv interpreter in this tree")
    r = _run_gate(_fake_tree(tmp_path, venv_py.resolve()), tmp_path / "out")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NO PYTEST SUMMARY" not in r.stdout
    assert "1 passed" in r.stdout
    assert "real: 0" in r.stdout
