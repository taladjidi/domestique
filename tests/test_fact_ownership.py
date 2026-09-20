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


def _config_aliases(tree: ast.AST) -> set:
    """Every name that refers to the config module in this file.

    `import config as c` then `c.ATHLETE_FTP_W = v` switches the proxy off
    exactly as `config.X = v` does, and a scanner that matches the literal
    name "config" waves it through.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "config":
                    names.add(alias.asname or "config")
    return names or {"config"}


def _proxied_target(node, aliases) -> str:
    """The proxied name in `config.X`, or "" for anything else."""
    if (isinstance(node, ast.Attribute) and node.attr in PROXIED
            and isinstance(node.value, ast.Name) and node.value.id in aliases):
        return node.attr
    return ""


def _setattr_target(node, aliases) -> str:
    """The proxied name in `setattr(config, "X", v)`."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "setattr" and len(node.args) >= 2):
        return ""
    target, name = node.args[0], node.args[1]
    if (isinstance(target, ast.Name) and target.id in aliases
            and isinstance(name, ast.Constant) and name.value in PROXIED):
        return name.value
    return ""


def _own_scope(body) -> list:
    """The statements of ONE scope: this body, minus nested function bodies.

    ast.walk with `continue` on a FunctionDef skips the node and still walks
    its children, because walk queues them before the loop body runs. That is
    how the first version of this scanner claimed to be scope-aware while a
    `del` inside a nested helper cancelled its parent's assignment -- the one
    shape the docstring said it handled.
    """
    out = []
    stack = list(body)
    while stack:
        node = stack.pop()
        out.append(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda, ast.ClassDef)):
                continue          # its own scope; scanned separately
            stack.append(child)
    return out


def _scope_hits(body, aliases) -> "tuple[dict, set]":
    """Assignments and deletions of proxied names in one scope."""
    assigned, deleted = {}, set()
    for node in _own_scope(body):
        targets = []
        if isinstance(node, (ast.Assign,)):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif isinstance(node, ast.Call):
            name = _setattr_target(node, aliases)
            if name:
                assigned.setdefault(name, node.lineno)
        elif isinstance(node, ast.Delete):
            for t in node.targets:
                name = _proxied_target(t, aliases)
                if name:
                    deleted.add(name)
        for t in targets:
            for element in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]):
                name = _proxied_target(element, aliases)
                if name:
                    assigned.setdefault(name, getattr(node, "lineno", 0))
    return assigned, deleted


def _unpaired_config_assignments(tree: ast.AST) -> list:
    """(line, name) for every write to a proxied name that its own scope never
    deletes.

    Assigning one creates a real module attribute, and Python then stops
    consulting config.__getattr__ for that name -- for the life of the
    process, across profile switches, with nothing to clear it.
    """
    aliases = _config_aliases(tree)
    scopes = [tree.body] if isinstance(tree, ast.Module) else []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append(node.body)
    out = []
    for body in scopes:
        assigned, deleted = _scope_hits(body, aliases)
        out += [(line, name) for name, line in assigned.items()
                if name not in deleted]
    return sorted(set(out))


# Facts that have no single accessor yet, each one a piece of work rather
# than a mistake. Pinned so the list can shrink but never grow quietly: a new
# fact arriving without an owner fails the test below.
OWNERLESS = set()          # every fact has an owner


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
    for path in sorted(SRC.rglob("*.py")):   # src/scripts/ too
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, name in _unpaired_config_assignments(tree):
            offenders.append(f"{path.name}:{line} assigns config.{name}")
    assert offenders == [], (
        "\n".join(offenders) + "\n\nA real module attribute shadows "
        "config.__getattr__, so the profile stops owning the value. Write the "
        "profile instead (the proxy reflects it on the next read), or `del` "
        "the attribute in a finally as training.py does.")


# Every way of writing the bug that a reviewer found the first scanner waving
# through. Each one switches the proxy off exactly as `config.X = v` does.
EVASIONS = {
    "del in a NESTED function does not cancel the parent's assignment": (
        "import config\n"
        "def outer(v):\n"
        "    config.ATHLETE_FTP_W = v\n"
        "    def inner():\n"
        "        del config.ATHLETE_FTP_W\n"
        "    return inner\n"),
    "setattr": (
        "import config\n"
        "def f(v): setattr(config, 'ATHLETE_FTP_W', v)\n"),
    "aliased import": (
        "import config as c\n"
        "def f(v): c.ATHLETE_FTP_W = v\n"),
    "augmented assignment": (
        "import config\n"
        "def f(v): config.ATHLETE_FTP_W += v\n"),
    "tuple target": (
        "import config\n"
        "def f(v): config.ATHLETE_FTP_W, other = v, 1\n"),
}


@pytest.mark.parametrize("name", sorted(EVASIONS))
def test_the_scanner_is_not_fooled(name):
    """A checker nobody has tried to fool is not known to work. Each of these
    passed the first version of this scanner, including the one its own
    docstring claimed to handle: `ast.walk` with `continue` on a FunctionDef
    skips the node and walks its children anyway."""
    assert _unpaired_config_assignments(ast.parse(EVASIONS[name])), (
        f"the scanner waved through: {name}")


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
