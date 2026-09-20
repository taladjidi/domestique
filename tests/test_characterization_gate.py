"""The characterization harness, run by the gate.

`tests/characterize_planner.py` compares 209 planner cases against a blessed
golden and exits 1 when any of them moves. It was run by NOTHING: its filename
matches neither half of pytest's default `python_files`, `pytest.ini` sets no
override, `tools/gate.sh` and the CI workflow both glob `tests/test_*.py`, and
no script referenced it. So the golden went stale on 2026-09-16, a replan
commit changed twenty cases and introduced a new invariant hit, and every
"characterization byte-identical" line written afterwards was true only of the
day it was written.

A gate nobody runs is a comment. This file is the two lines that make it one:
31 seconds for the full 209, which is worth paying on every run.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "characterize_planner.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HARNESS), *args],
        cwd=str(ROOT), capture_output=True, text=True, timeout=1800)


def test_the_planner_still_produces_the_blessed_plans():
    done = _run()
    assert done.returncode == 0, (
        "the planner's output moved away from tests/characterization.json:\n\n"
        + done.stdout[-4000:]
        + "\n\nIf every change above is intended, re-run with --bless AND say "
          "in the commit message which behaviour changed and why. Blessing "
          "without reading is how twenty cases and an invariant hit went "
          "unnoticed for four days.")


def test_the_harness_can_still_tell_when_something_moves():
    """Its own self-test: perturb each entry point and check the cases that
    entry point owns actually move. A characterization that cannot see a
    change is a file of numbers."""
    done = _run("--self-test")
    assert done.returncode == 0, done.stdout[-3000:]
