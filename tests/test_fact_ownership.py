"""The registry in src/facts.py has to match the code, or it is decoration.

Three checks, each for a mistake this project has actually shipped:

  * an accessor that does not exist -- a row claiming an owner that moved;
  * two different facts sharing one key name in one scope, which is the
    week-load bug: `tss_target` meant the ramp's budget in /api/plan and the
    sum of the prescribed sessions everywhere else, so the rollup divided one
    by the other and read 499/138;
  * a module-level assignment to a name `config` resolves through the profile,
    which switches the proxy off permanently -- the FTP bug. training.py does
    assign two of them deliberately and `del`s them in a `finally`, so the
    rule is "assign only if the same function deletes", not "never assign".

The static check has its own negative control: a known-bad module is fed to
the same scanner and the test fails if the scanner passes it.
"""
from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import facts                                          # noqa: E402

SRC = ROOT / "src"
# The names config resolves through ProfileManager (config.py __getattr__).
PROXIED = {"ATHLETE_FTP_W", "ATHLETE_WEIGHT_KG", "ATHLETE_LBM_KG",
           "ATHLETE_LTHR", "ATHLETE_MAX_HR", "HRV_BASELINE_MEAN",
           "HRV_BASELINE_SD", "RHR_BASELINE", "ICU_ATHLETE_ID",
           "ICU_API_KEY", "ICU_ACCESS_TOKEN"}


def _proxied_target(node) -> str:
    """The proxied name in `config.X`, or "" for anything else."""
    if (isinstance(node, ast.Attribute) and node.attr in PROXIED
            and isinstance(node.value, ast.Name) and node.value.id == "config"):
        return node.attr
    return ""


def _scope_hits(body) -> "tuple[dict, set]":
    """Assignments and deletions of proxied names in ONE scope.

    Nested functions are their own scope: a `del` inside a helper does not
    undo an assignment made by its parent, and counting them together is how
    a scanner reports a violation twice, or misses one.
    """
    assigned, deleted = {}, set()
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    name = _proxied_target(t)
                    if name:
                        assigned.setdefault(name, node.lineno)
            elif isinstance(node, ast.Delete):
                for t in node.targets:
                    name = _proxied_target(t)
                    if name:
                        deleted.add(name)
    return assigned, deleted


def _unpaired_config_assignments(tree: ast.AST) -> list:
    """(line, name) for every `config.X = ...` whose own scope never `del`s it.

    Assigning one of these creates a real module attribute, and Python then
    stops consulting config.__getattr__ for that name -- for the life of the
    process, across profile switches, with nothing to clear it.
    """
    scopes = [tree.body] if isinstance(tree, ast.Module) else []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append(node.body)
    out = []
    for body in scopes:
        assigned, deleted = _scope_hits(body)
        out += [(line, name) for name, line in assigned.items()
                if name not in deleted]
    return sorted(set(out))


# Facts that have no single accessor yet, each one a piece of work rather
# than a mistake. Pinned so the list can shrink but never grow quietly: a new
# fact arriving without an owner fails the test below.
OWNERLESS = {"week_done"}


def test_every_accessor_in_the_registry_exists():
    missing = []
    for fact in facts.FACTS.values():
        if not fact.accessor:
            continue
        parts = fact.accessor.split(".")
        # "ProfileManager.ftp" names a class first; "week_view.WeekView.budget"
        # names its module first.
        module_name = {"ProfileManager": "profile_manager"}.get(parts[0], parts[0])
        try:
            module = __import__(module_name)
        except ImportError:
            missing.append(f"{fact.name}: no module {module_name}")
            continue
        target = module
        if parts[0] != module_name:            # the accessor started at a class
            target = getattr(module, parts[0], None)
            if target is None:
                missing.append(f"{fact.name}: {module_name} has no {parts[0]}")
                continue
        for part in parts[1:]:
            nxt = getattr(target, part, None)
            if nxt is None:
                # a dataclass field is an annotation, not an attribute
                if part in getattr(target, "__annotations__", {}):
                    nxt = object()
                elif part in getattr(target, "__dataclass_fields__", {}):
                    nxt = object()
            if nxt is None:
                missing.append(f"{fact.name}: {fact.accessor} not found")
                break
            target = nxt
    assert missing == [], "\n".join(missing)


def test_the_facts_without_an_owner_are_the_known_ones():
    """The gap, pinned. Shrinking this set is the work; growing it silently is
    how the app got here."""
    ownerless = {name for name, f in facts.FACTS.items() if not f.owner}
    assert ownerless == OWNERLESS, (
        f"facts with no owner: {sorted(ownerless)}; expected {sorted(OWNERLESS)}")


def test_no_two_facts_share_a_name_in_one_scope():
    """One key name, one question. This is the check that would have caught
    the week-load bug before a rider did."""
    seen = defaultdict(list)
    for fact in facts.FACTS.values():
        for key in fact.api_keys:
            seen[(fact.scope, key)].append(fact.name)
    clashes = {k: v for k, v in seen.items() if len(v) > 1}
    assert clashes == {}, (
        f"the same key name answers two questions in one scope: {clashes}. "
        f"Readers cannot tell them apart, and neither can the probe.")


def test_no_module_switches_off_the_config_proxy():
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, name in _unpaired_config_assignments(tree):
            offenders.append(f"{path.name}:{line} assigns config.{name}")
    assert offenders == [], (
        "\n".join(offenders) + "\n\nA real module attribute shadows "
        "config.__getattr__, so the profile stops owning the value. Write the "
        "profile instead (the proxy reflects it on the next read), or `del` "
        "the attribute in a finally as training.py does.")


def test_the_scanner_catches_a_switch_off_that_is_planted():
    """The control. A checker nobody has fooled is not known to work."""
    planted = ast.parse(
        "import config\n"
        "def save(ftp):\n"
        "    config.ATHLETE_FTP_W = ftp\n")
    assert _unpaired_config_assignments(planted) == [(3, "ATHLETE_FTP_W")], (
        "the scanner passed a module that assigns a proxied name")

    paired = ast.parse(
        "import config\n"
        "def swap(key):\n"
        "    config.ICU_API_KEY = key\n"
        "    try:\n"
        "        pass\n"
        "    finally:\n"
        "        del config.ICU_API_KEY\n")
    assert _unpaired_config_assignments(paired) == [], (
        "the scanner flagged the assign-then-del pattern that is correct")
