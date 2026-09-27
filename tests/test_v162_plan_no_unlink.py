"""v1.6.2 — delete-protection guard.

After v1.6.2 every write to ``current_plan.json`` goes through the plan
store (``plan_store.update`` since the one-plan round; it rotates backups
and only performs an atomic ``Path.replace``). No source file outside it
may call ``.unlink()`` or ``os.remove()`` on a path referencing the live
plan file. (plan_store is the one place that may: its rotation drops the
oldest .bak by design. The G4 scan, tests/test_one_plan_static.py, is the
full rule; this regex is the v1.6.2 original.)

This test scans the source. It is intentionally strict: any new direct
``.unlink()`` on a plan-path variable will fail it.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES = ("app.py", "training_planner.py")

# Variable names commonly bound to the live plan file.
PLAN_VAR_RE = re.compile(
    r"\b(?:json_path|plan_path|_plan_json|_plan_path|current_plan)\b"
)
UNLINK_RE = re.compile(r"\.unlink\s*\(\s*\)")
REMOVE_RE = re.compile(r"os\.remove\s*\(")
ALLOWED_HELPERS: set = set()


def _function_at_line(lines: list[str], lineno: int) -> str:
    """Return the name of the function/method enclosing 1-indexed lineno."""
    for i in range(lineno - 1, -1, -1):
        line = lines[i]
        m = re.match(r"\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if m:
            return m.group(1)
    return "<module>"


def _scan(src_file: Path) -> list[tuple[int, str, str]]:
    """Return ``[(lineno, line, enclosing_function), ...]`` for every
    .unlink() or os.remove() call on a plan-path variable in ``src_file``.
    """
    lines = src_file.read_text(encoding="utf-8").splitlines()
    hits: list[tuple[int, str, str]] = []
    for idx, line in enumerate(lines, start=1):
        # Strip end-of-line comments to avoid matching docstring narrative.
        code = line.split("#", 1)[0]
        if not (UNLINK_RE.search(code) or REMOVE_RE.search(code)):
            continue
        if not PLAN_VAR_RE.search(code):
            continue
        fn = _function_at_line(lines, idx)
        hits.append((idx, line.rstrip(), fn))
    return hits


def test_no_unlink_on_plan_path_outside_sanctioned_helpers() -> None:
    bad: list[str] = []
    for fname in SOURCES:
        path = REPO_ROOT / "src" / fname
        for lineno, line, fn in _scan(path):
            if fn in ALLOWED_HELPERS:
                continue
            bad.append(f"{fname}:{lineno} ({fn}): {line.strip()}")

    assert not bad, (
        "v1.6.2 forbids direct .unlink()/os.remove() on the plan path "
        "outside plan_store. All plan writes go through plan_store.update. "
        "Offending sites:\n  " + "\n  ".join(bad)
    )


def test_app_writes_the_plan_through_the_store_only() -> None:
    """app.py writes the plan through plan_store.update -- no retired
    primitive, no inline tmp+rename plan-write block."""
    app_src = (REPO_ROOT / "src" / "app.py").read_text(encoding="utf-8")
    assert "plan_store.update" in app_src
    for retired in ("tp.atomic_write_plan", "tp.update_plan", "tp.plan_write_lock",
                    "_rotate_plan_backups"):
        assert retired not in app_src, retired
    inline_pat = re.compile(
        r"tmp_path\s*=\s*json_path\.with_suffix\(['\"]\.tmp['\"]\)"
    )
    assert not inline_pat.findall(app_src)
