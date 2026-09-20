"""Import cycles, counting the imports that hide inside functions.

At module level this package is acyclic and always has been, which is the
reassuring half of the picture. The other half: 250-odd imports sit inside
function bodies, most of them written that way to dodge a cycle, and counting
them the package had ONE strongly connected component of 19 modules, 74% of
src. A deferred import is still a dependency; it just fails at call time
instead of import time, and it hides the shape of the thing from anyone
reading the file.

One line was half of it. `training_planner._zwo_scanner` did
`import app; return app._scan_zwo_for_library`, and app does not own that
name: it imports it from the leaf `zwo_parse`, which training_planner already
imports at module scope. 19 modules became 8.

The remaining components are pinned below rather than described, so the
number can go down and not up.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# What is still tangled, and why each one is work rather than a mistake:
#
#   the planner core -- config, db, power_curve, profile_manager, ride_storage,
#   training, training_planner, week_plan. Profile-scoped storage and the
#   planner read each other; untangling needs the store boundary that
#   src/facts.py names.
#
#   app <-> icu_calendar_push -- the push reads the plan and app serves it.
EXPECTED_CYCLES = [
    ["app", "icu_calendar_push"],
    ["config", "db", "power_curve", "profile_manager", "ride_storage",
     "training", "training_planner", "week_plan"],
]


def _modules() -> dict:
    return {p.stem: p for p in SRC.glob("*.py")}


def _graph(include_deferred: bool) -> dict:
    mods = _modules()
    out = {m: set() for m in mods}
    for name, path in mods.items():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        inside_a_function = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    inside_a_function.add(id(sub))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if id(node) in inside_a_function and not include_deferred:
                continue
            targets = ([a.name.split(".")[0] for a in node.names]
                       if isinstance(node, ast.Import)
                       else [(node.module or "").split(".")[0]])
            out[name].update(t for t in targets if t in mods and t != name)
    return out


def _cycles(graph: dict) -> list:
    """Tarjan, iterative: the recursive form overflows on this graph."""
    index, low, on_stack, stack, result, counter = {}, {}, set(), [], [], [0]
    for root in list(graph):
        if root in index:
            continue
        work = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter[0]; counter[0] += 1
        stack.append(root); on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter[0]; counter[0] += 1
                    stack.append(child); on_stack.add(child)
                    work.append((child, iter(sorted(graph[child]))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    popped = stack.pop(); on_stack.discard(popped)
                    component.append(popped)
                    if popped == node:
                        break
                if len(component) > 1:
                    result.append(sorted(component))
    return sorted(result)


def test_there_are_no_module_level_cycles():
    """The import that runs at import time. A cycle here is an ImportError
    waiting for the first person to import the modules in the other order."""
    assert _cycles(_graph(include_deferred=False)) == []


def test_the_deferred_cycles_are_the_known_ones():
    found = _cycles(_graph(include_deferred=True))
    assert found == sorted(EXPECTED_CYCLES), (
        f"import cycles changed.\n  found:    {found}\n"
        f"  expected: {sorted(EXPECTED_CYCLES)}\n"
        f"A function-local import is still a dependency. If you untangled one, "
        f"strike it from EXPECTED_CYCLES; if you added one, the module you "
        f"reached for probably re-exports something from a leaf you can import "
        f"directly, which is what made the 19-module component an 8.")


def test_the_planner_does_not_import_the_app():
    """The specific edge that was half the problem, kept cut."""
    source = (SRC / "training_planner.py").read_text(encoding="utf-8")
    offenders = [line.strip() for line in source.splitlines()
                 if line.strip() in ("import app", "import app as _app")]
    assert offenders == [], (
        f"training_planner imports app again: {offenders}. app re-exports the "
        f"library scanner from zwo_parse, which this module already imports; "
        f"reach for the leaf.")


def test_the_detector_sees_a_cycle_that_hides_in_a_function(tmp_path):
    """The control: a cycle written ONLY as function-local imports must be
    caught, and a clean pair must not be."""
    global SRC
    real = SRC
    try:
        SRC = tmp_path
        (tmp_path / "alpha.py").write_text("def use():\n    import beta\n    return beta\n")
        (tmp_path / "beta.py").write_text("def thing():\n    import alpha\n    return alpha\n")
        assert _cycles(_graph(include_deferred=False)) == []
        assert _cycles(_graph(include_deferred=True)) == [["alpha", "beta"]]

        (tmp_path / "beta.py").write_text("def thing():\n    return 1\n")
        assert _cycles(_graph(include_deferred=True)) == []
    finally:
        SRC = real
