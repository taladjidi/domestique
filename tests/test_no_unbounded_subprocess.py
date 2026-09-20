"""No call in this repo may wait on a child process forever.

WHY. The pull-request gate is `timeout-minutes: 120` on a macOS runner, split
over two shards. A shard that stops making progress does not fail -- it rides
to the two-hour wall and is reported as CANCELLED, which reads like somebody
pressed a button. It burns two hours of runner time per push and says nothing
about which test stopped. Two hangs in September were reported that way.

`--timeout=900` is already on the pytest command line and does not close this:
the signal method cannot always interrupt a blocked child wait, and a test
killed mid-`subprocess.run` leaves the child orphaned anyway (the runner's
"Terminate orphan process: pid (2833) (Python)" line).

So every blocking child wait carries a deadline, and this test is what keeps
it that way. Twelve calls had none when it was written: four node harnesses
(two of which also had no guard for node being absent, so a runner without
node raised FileNotFoundError instead of skipping), a `git ls-files`, a
`which node` spawned at COLLECTION time inside a skipif decorator, two pip
downloads and three Windows tools that can block on a policy prompt.

WHAT IS NOT FLAGGED, and why the detector has to know the difference:
`threading.Event.wait()` is not a child wait. src/db.py, src/log_config.py and
src/launcher.py call it -- twice with a positional timeout, twice as a
deliberate block-until-shutdown -- and a detector that cannot tell an Event
from a Popen reports all four and gets itself switched off. Only a name this
module actually assigned from subprocess.Popen(...) is treated as a handle.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("src", "tests", "tools", "packaging")
SKIP_PARTS = {".venv", "node_modules", "build", "dist", "__pycache__"}

# subprocess functions that BLOCK until the child exits.
BLOCKING = {"run", "call", "check_call", "check_output"}


def _has_kw(call: ast.Call, name: str) -> bool:
    return any(k.arg == name for k in call.keywords)


def _is_popen(node) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Popen")


def _bind(target, value, out: set[str]) -> None:
    """Record ``target`` if ``value`` is a Popen, walking tuples in step.

    ``a, b = subprocess.Popen(...), 1`` binds a Tuple, not a Call -- the first
    version only looked at a direct Call and missed the handle entirely. Its
    own control caught that, which is the whole reason the controls are here.
    """
    if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple):
        for t_, v_ in zip(target.elts, value.elts):
            _bind(t_, v_, out)
        return
    if _is_popen(value):
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name):
                out.add(sub.id)


def _popen_names(tree: ast.AST) -> set[str]:
    """Names bound to a subprocess.Popen(...) anywhere in the module."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t_ in node.targets:
                _bind(t_, node.value, out)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _bind(node.target, node.value, out)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    _bind(item.optional_vars, item.context_expr, out)
    return out


def _offenders(tree: ast.AST, rel: str) -> list[str]:
    handles = _popen_names(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute):
            continue
        base = getattr(fn.value, "id", None)
        if base == "subprocess" and fn.attr in BLOCKING and not _has_kw(node, "timeout"):
            found.append(f"{rel}:{node.lineno} subprocess.{fn.attr}(...) has no timeout=")
        elif (fn.attr in ("wait", "communicate") and base in handles
              and not _has_kw(node, "timeout") and not node.args):
            found.append(f"{rel}:{node.lineno} {base}.{fn.attr}() has no timeout=")
        elif base == "os" and fn.attr in ("system", "popen"):
            found.append(f"{rel}:{node.lineno} os.{fn.attr}() cannot be bounded; "
                         f"use subprocess with timeout=")
    return found


def _python_files():
    for top in SCANNED:
        d = ROOT / top
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.py")):
            if SKIP_PARTS & set(p.parts):
                continue
            yield p


def test_every_child_wait_has_a_deadline():
    offenders = []
    for p in _python_files():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        offenders += _offenders(tree, str(p.relative_to(ROOT)))
    assert offenders == [], (
        "unbounded child wait(s) -- these ride the 120-minute job wall and are "
        "reported as CANCELLED, not as a failure:\n  "
        + "\n  ".join(offenders))


# ── the detector's own controls ──────────────────────────────────────────────

_CAUGHT = [
    ("bare run", "import subprocess\nsubprocess.run(['x'])\n"),
    ("check_output", "import subprocess\nsubprocess.check_output(['x'])\n"),
    ("call", "import subprocess\nsubprocess.call(['x'])\n"),
    ("popen wait", "import subprocess\np = subprocess.Popen(['x'])\np.wait()\n"),
    ("popen communicate",
     "import subprocess\nproc = subprocess.Popen(['x'])\nproc.communicate()\n"),
    ("os.system", "import os\nos.system('x')\n"),
    ("tuple-assigned popen",
     "import subprocess\na, b = subprocess.Popen(['x']), 1\na.wait()\n"),
    ("with-statement popen",
     "import subprocess\nwith subprocess.Popen(['x']) as p:\n    p.communicate()\n"),
]

_IGNORED = [
    ("run with timeout", "import subprocess\nsubprocess.run(['x'], timeout=30)\n"),
    ("popen wait with timeout",
     "import subprocess\np = subprocess.Popen(['x'])\np.wait(timeout=2)\n"),
    ("popen wait positional",
     "import subprocess\np = subprocess.Popen(['x'])\np.wait(2)\n"),
    # the four real Event.wait shapes in src/ that must never be reported
    ("event wait bare",
     "import threading\n_shutdown = threading.Event()\n_shutdown.wait()\n"),
    ("event wait positional",
     "import threading\n_stop = threading.Event()\n_stop.wait(sleep_for)\n"),
    ("event wait in a loop",
     "import threading\n_f = threading.Event()\nwhile not _f.wait(1.0):\n    pass\n"),
    ("popen never waited (sleep_inhibit's caffeinate)",
     "import subprocess\n_proc = subprocess.Popen(['caffeinate'])\n_proc.poll()\n"),
]


@pytest.mark.parametrize("label,src", _CAUGHT, ids=[c[0] for c in _CAUGHT])
def test_the_detector_catches_it(label, src):
    """A rule with no test that fails when the rule is removed is a comment."""
    assert _offenders(ast.parse(src), "probe.py"), f"missed: {label}"


@pytest.mark.parametrize("label,src", _IGNORED, ids=[c[0] for c in _IGNORED])
def test_the_detector_leaves_these_alone(label, src):
    """A detector that cries wolf on threading.Event gets switched off, and
    then it is guarding nothing."""
    assert _offenders(ast.parse(src), "probe.py") == [], f"false positive: {label}"


# ── and a child that is not installed must SKIP, not error ───────────────────

def _spawns_node(tree: ast.AST) -> list[int]:
    """Lines where this module hands 'node' to subprocess."""
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and getattr(node.func.value, "id", None) == "subprocess"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        head = None
        if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
            head = first.elts[0]
        elif isinstance(first, ast.Constant):
            head = first
        if isinstance(head, ast.Constant) and head.value == "node":
            out.append(node.lineno)
    return out


# Any of these means the file has thought about node being absent.
_GUARDS = ("needs_node", "_require_node", 'which("node")', "which('node')")


def test_a_js_harness_skips_when_node_is_missing():
    """tests.yml installs Python and nothing else; node is present on the
    GitHub macOS image by luck, not by declaration. An unguarded harness turns
    "node is not here" into FileNotFoundError, which reads as a real failure
    and hides that the assertion never ran.

    Two files spawned node with no guard at all when this was written.
    """
    offenders = []
    for p in sorted((ROOT / "tests").glob("*.py")):
        text = p.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = _spawns_node(tree)
        if lines and not any(g in text for g in _GUARDS):
            offenders.append(f"{p.name}: spawns node at line(s) "
                             f"{', '.join(map(str, lines))} with no guard")
    assert offenders == [], (
        "a JS harness must skip when node is absent, not raise "
        "FileNotFoundError:\n  " + "\n  ".join(offenders))


def test_the_node_guard_check_can_tell_the_difference(tmp_path):
    """Its control: an unguarded harness must be caught, a guarded one must
    not, and a file that never touches node must not be dragged in."""
    unguarded = ast.parse('import subprocess\nsubprocess.run(["node", "-e", "1"], timeout=5)\n')
    assert _spawns_node(unguarded) == [2]
    assert _spawns_node(ast.parse('import subprocess\nsubprocess.run(["git", "status"], timeout=5)\n')) == []
    assert _spawns_node(ast.parse('x = 1\n')) == []
    guarded = 'import shutil\nneeds_node = 1\nsubprocess.run(["node"], timeout=5)\n'
    assert any(g in guarded for g in _GUARDS)
