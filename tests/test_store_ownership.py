"""Who touches each store, measured — and the list may only shrink.

`src/facts.py` names an owner for every fact: the module allowed to write its
store, and the one every other reader should go through. Nothing enforced it.
The header of facts.py claimed a static test "fails when a module that is not
the owner touches the store"; there was no such test, which is how rows like
`ctl: owner=fitness` survived while `fitness` writes nothing at all and five
other modules write the wellness table.

This is that test, and it does not pretend the codebase already obeys. It
measures who touches each store today and compares against a pinned census.
A NEW module touching a store fails. A pinned entry that disappears also
fails, so the list shrinks deliberately, one commit at a time, and cannot
quietly grow back.

Matching is textual on purpose. An AST scan of "who writes this SQLite table"
would need to follow strings into cursors and helpers; the patterns below are
what a reader greps for, they are checked by a planted-violation control at
the bottom, and a false positive costs one line in the census rather than a
missed bug.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import facts                                          # noqa: E402

SRC = ROOT / "src"
# `error_codes.py` is a catalogue of human-readable descriptions: it names
# current_plan.json and the wellness table in message strings and touches
# neither. Textual matching cannot tell a sentence from a statement, so the
# one module that is all sentences is excluded by name.
NOT_CODE_ABOUT_STORES = {"error_codes.py"}

# How touching each store looks in source. One entry per store in the registry.
STORE_PATTERNS = {
    "sqlite:wellness": re.compile(
        r"(FROM|INTO|UPDATE|JOIN)\s+wellness\b", re.I),
    "profile:athlete.json": re.compile(r"athlete\.json|_athlete\["),
    "plan:current_plan.json": re.compile(
        r"atomic_write_plan|current_plan\.json"),
    "profile:rides/": re.compile(
        r"_icu_rides_dir|_fit_rides_dir|rides/icu|rides/fit"),
    # Calls, not names: training_planner has a LOCAL VARIABLE called
    # latest_eftp in its drift code, and a census that counts variable names
    # accuses a module of touching a store it never opens.
    "profile:wellness/": re.compile(
        r"(load_recent_wellness|_wellness_dir|latest_eftp)\s*\("),
    "routes:routes.json": re.compile(r"routes\.json|_load_routes_v2"),
}

# WHO TOUCHES WHAT TODAY. Every line is work to do, not a permission granted:
# the fact's owner is where the rule should live. Shrinking this census is the
# "one owner per fact" work; the registry's `owner` field says where each is
# going.
CENSUS = {
    "sqlite:wellness": {
        "db.py",                    # the sync writes it: the intended owner of the row
        "fitness.py",               # reads it through the ladder -- the fact's owner
        "app.py",                   # endpoints read and write it directly
        "readiness_composite.py",   # reads raw rows for its history
        "migrate_profiles.py",      # one-time migration
    },
    "profile:athlete.json": {
        "profile_manager.py",       # the owner: _set_fact gates every write
        "app.py", "db.py", "training_planner.py", "migrate_profiles.py",
        "facts.py",                 # names the store in the registry
    },
    "plan:current_plan.json": {
        "plan_store.py",            # the owner (contract 10.1): every read and write
        "app.py",                   # the Raw-plan markdown beside it (10.4)
        "facts.py",
    },
    "profile:rides/": {
        "ride_storage.py",          # the owner
        "app.py", "db.py", "power_curve.py", "paths.py",
    },
    "profile:wellness/": {
        "ride_storage.py",          # the owner: latest_eftp reads it here
        "app.py",                   # settings and rider-stats read through it
    },
    "routes:routes.json": {
        "routes_lib.py",            # the owner
        "app.py",                   # /api/routes and /api/courses read it
        "paths.py", "facts.py",
    },
}


def _touchers(store: str) -> set:
    pattern = STORE_PATTERNS[store]
    out = set()
    for path in sorted(SRC.glob("*.py")):
        if path.name in NOT_CODE_ABOUT_STORES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Comments and docstrings describe stores all the time; only count a
        # line that is code.
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if pattern.search(line):
                out.add(path.name)
                break
    return out


def test_every_store_in_the_registry_has_a_census():
    stores = {f.store for f in facts.FACTS.values()}
    missing = sorted(s for s in stores if s not in STORE_PATTERNS)
    assert missing == [], (
        f"{missing} appear in facts.py with no way to see who touches them; "
        f"a store nobody can measure cannot have an owner")


def test_no_module_starts_touching_a_store_it_did_not():
    grew = {}
    for store, expected in CENSUS.items():
        actual = _touchers(store)
        new = actual - expected
        if new:
            grew[store] = sorted(new)
    assert grew == {}, (
        f"modules newly reaching into a store: {grew}. The owner is in "
        f"facts.py; go through its accessor, or add the module to the census "
        f"with a reason if it genuinely belongs there.")


def test_the_census_shrinks_and_does_not_lie():
    """A pinned entry that no longer touches the store is progress, and has to
    be removed from the census so the next reader sees the truth."""
    stale = {}
    for store, expected in CENSUS.items():
        actual = _touchers(store)
        gone = expected - actual
        if gone:
            stale[store] = sorted(gone)
    assert stale == {}, (
        f"these no longer touch the store and should leave the census: "
        f"{stale}. That is one owner per fact getting closer -- record it.")


def test_the_owner_of_each_fact_is_one_of_the_modules_that_touch_its_store():
    """A row whose owner does not appear in the census is fiction: `ctl` said
    owner=fitness while fitness wrote nothing, and nothing noticed for as long
    as nobody checked."""
    wrong = []
    for fact in facts.FACTS.values():
        if not fact.owner or fact.derived:
            # A derived fact's owner computes it from a store someone else
            # keeps. That is legitimate and has to be declared, not assumed.
            continue
        module = f"{fact.owner}.py"
        if module not in CENSUS.get(fact.store, set()):
            wrong.append(f"{fact.name}: owner {fact.owner} never touches {fact.store}")
    assert wrong == [], "\n".join(wrong)


def test_the_scanner_sees_a_planted_touch(tmp_path):
    """The control. A census that cannot notice a new toucher is decoration."""
    planted = SRC / "_planted_ownership_probe.py"
    planted.write_text(
        "def naughty(conn):\n"
        "    return conn.execute('SELECT ctl FROM wellness').fetchone()\n",
        encoding="utf-8")
    try:
        assert "_planted_ownership_probe.py" in _touchers("sqlite:wellness"), (
            "the scanner missed a module that reads the wellness table")
    finally:
        planted.unlink()
