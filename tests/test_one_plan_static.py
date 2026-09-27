"""G4 (contract 10.6): the plan is touched by plan_store.py alone -- read in
the source.

tests/plan_scan.py lists every place in src/ that builds a plan path, names a
plan-write primitive, writes a plan file raw, uses a read-modify-write other
than plan_store.update, or reaches a plan writer from an ``async def``
without leaving the event loop. The list below is what 433c6067 (production
2026-09-27) still had: the round's worklist. It may only shrink -- a new
finding fails the first test, a fixed one that stays listed fails the
second -- and it ends empty.

The scanner's self-test is the single-source audit's planted violations
(the 12 the evaluator planted, the four extra), both in their original form
against the retired training_planner primitives and rewritten against
plan_store; the negative control -- the one door, called off the event loop
-- must stay clean.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import plan_scan

SRC = Path(__file__).resolve().parent.parent / "src"

# The worklist: what is left of the 202 findings at 433c6067 (keyed "KIND
# module:function  what"). The two BRIDGE entries are the one addition: the
# legacy plan-dir seam the suite still patches, removed with 10.3.
ALLOWED: Counter = Counter({
    'ACCESS app:_bridge_plan_dir  _plan_dir()': 1,   # BRIDGE, gone with 10.3
    'ACCESS paths:<module>  _DEFAULT_PLAN_DIR': 1,
    "ACCESS paths:<module>  path / 'plans'": 1,
    'ACCESS paths:_plan_dir  _DEFAULT_PLAN_DIR': 1,
    'ACCESS profile_manager:ProfileManager.apply_training_dirs  PLAN_DIR': 1,
    "ACCESS profile_manager:ProfileManager.plan_dir  path / 'plans'": 1,
    'ACCESS training_planner:<module>  PLAN_DIR': 1,
    "ACCESS training_planner:<module>  path / 'plans'": 1,
    'OWNER app:<module>  plan_store._BRIDGE': 1,   # BRIDGE, gone with 10.3
    'OWNER profile_manager:ProfileManager.plan_dir  raw Path.mkdir': 1,
})


@pytest.fixture(scope="module")
def found() -> Counter:
    return plan_scan.scan(SRC)


def test_no_plan_access_outside_the_store(found):
    new = found - ALLOWED
    assert not new, "plan touched outside plan_store:\n  " + "\n  ".join(sorted(new.elements()))


def test_the_worklist_only_shrinks(found):
    stale = ALLOWED - found
    assert not stale, ("fixed -- delete from ALLOWED:\n  "
                       + "\n  ".join(sorted(stale.elements())))


# ── the planted violations ───────────────────────────────────────────────────
# Appended to app.py's source, one at a time; each must produce a finding in
# the plant's own function.
LEGACY_PLANTS = {
    "a_direct_write": '''
async def _plant_handler(request):
    json_path = _plan_dir() / "current_plan.json"
    tp.atomic_write_plan(json_path, {"x": 1})
''',
    "b_with_lock": '''
async def _plant_handler(request):
    with tp.plan_write_lock():
        pass
''',
    "c_sync_helper": '''
def _plant_helper(p):
    tp.update_plan(p, lambda plan: plan)

async def _plant_handler(request):
    _plant_helper(_plan_dir() / "current_plan.json")
''',
    "d_alias_module": '''
import training_planner as _tp_alias

async def _plant_handler(request):
    _tp_alias.update_plan(_plan_dir() / "current_plan.json", lambda plan: plan)
''',
    "e_raw_lock_attr": '''
async def _plant_handler(request):
    with tp._plan_write_lock:
        pass
''',
    "f_nested_called_directly": '''
async def _plant_handler(request):
    def _w():
        tp.update_plan(_plan_dir() / "current_plan.json", lambda plan: plan)
    _w()
''',
    "g_other_module_helper": '''
async def _plant_handler(request):
    tp.rewrite_stale_plan_classifications(_plan_dir() / "current_plan.json")
''',
    "h_two_level": '''
def _plant_inner(p):
    tp.update_plan(p, lambda plan: plan)

def _plant_outer(p):
    _plant_inner(p)

async def _plant_handler(request):
    _plant_outer(_plan_dir() / "current_plan.json")
''',
    "i_name_alias": '''
_plant_alias = _maybe_auto_reforecast

async def _plant_handler(request):
    _plant_alias("default", 1)
''',
    "j_from_import": '''
async def _plant_handler(request):
    from training_planner import update_plan as _up
    _up(_plan_dir() / "current_plan.json", lambda plan: plan)
''',
    "k_existing_heal_direct": '''
async def _plant_handler(request):
    _heal_plan_on_disk()
''',
    "l_method_call": '''
class _PlantStore:
    def write(self, p):
        tp.update_plan(p, lambda plan: plan)

async def _plant_handler(request):
    _PlantStore().write(_plan_dir() / "current_plan.json")
''',
    "n_raw_open": '''
def _plant_writer():
    p = _plan_dir() / "current_plan.json"
    with open(p, "w") as f:
        json.dump({}, f)
''',
    "o_os_replace": '''
def _plant_writer(json_path):
    tmp = json_path.with_suffix(".tmp")
    os.replace(tmp, json_path)
''',
    "p_mutator_nested_write": '''
def _plant_writer():
    jp = _plan_dir() / "current_plan.json"
    def _m(plan):
        tp.atomic_write_plan(jp, plan)
        return None
    tp.update_plan(jp, _m)
''',
}

# The same violations against the one owner.
STORE_PLANTS = {
    "a_private_write": '''
async def _plant_handler(request):
    plan_store._commit(plan_store._paths("default"), {"x": 1})
''',
    "b_private_lock": '''
async def _plant_handler(request):
    with plan_store._LOCK:
        pass
''',
    "c_sync_helper": '''
def _plant_helper(pid):
    plan_store.update(pid, lambda plan: plan, reason="plant")

async def _plant_handler(request):
    _plant_helper("default")
''',
    "d_alias_module": '''
import plan_store as _ps_alias

async def _plant_handler(request):
    _ps_alias.update("default", lambda plan: plan, reason="plant")
''',
    "e_from_private": '''
from plan_store import _commit as _pc

def _plant_writer(paths, plan):
    _pc(paths, plan)
''',
    "f_nested_called_directly": '''
async def _plant_handler(request):
    def _w():
        plan_store.update("default", lambda plan: plan, reason="plant")
    _w()
''',
    "g_other_module_helper": '''
async def _plant_handler(request):
    tp.rewrite_stale_plan_classifications("default")
''',
    "h_two_level": '''
def _plant_inner(pid):
    plan_store.update(pid, lambda plan: plan, reason="plant")

def _plant_outer(pid):
    _plant_inner(pid)

async def _plant_handler(request):
    _plant_outer("default")
''',
    "i_name_alias": '''
_plant_alias = _maybe_auto_reforecast

async def _plant_handler(request):
    _plant_alias("default", 1)
''',
    "j_from_import": '''
async def _plant_handler(request):
    from plan_store import update as _up
    _up("default", lambda plan: plan, reason="plant")
''',
    "k_existing_heal_direct": '''
async def _plant_handler(request):
    _heal_plan_on_disk("default")
''',
    "l_method_call": '''
class _PlantStore:
    def write(self, pid):
        plan_store.update(pid, lambda plan: plan, reason="plant")

async def _plant_handler(request):
    _PlantStore().write("default")
''',
    "n_raw_open": '''
def _plant_writer(home):
    p = home / "profiles" / "default" / "plans" / "current_plan.json"
    with open(p, "w") as f:
        json.dump({}, f)
''',
    "o_os_replace": '''
def _plant_writer(json_path):
    tmp = json_path.with_suffix(".tmp")
    os.replace(tmp, json_path)
''',
    "p_mutator_nested_write": '''
def _plant_writer():
    def _m(plan):
        plan_store._commit(plan_store._paths("default"), plan)
        return None
    plan_store.update("default", _m, reason="plant")
''',
    "q_raw_read": '''
def _plant_reader(home):
    return json.loads((home / "profiles" / "default" / "plans" / "current_plan.json").read_text())
''',
    "r_backup_glob": '''
def _plant_reader(d):
    return sorted(d.glob("current_plan.json.bak*"))
''',
    "s_subprocess_copy": '''
def _plant_writer(prof_dir):
    subprocess.run(["cp", "x", str(prof_dir / "plans")])
''',
}

NEGATIVE_CONTROL = '''
async def _plant_handler(request):
    await run_in_threadpool(plan_store.update, "default", lambda plan: plan, reason="plant")
'''


def _plant_findings(code: str) -> list[str]:
    if "plan_store" in code:
        code = "import plan_store\n" + code
    return [plan_scan.key(f) for f in plan_scan.scan_full(SRC, {"app": code})
            if ("_plant" in f["func"] or "_Plant" in f["func"]) and f["kind"] != "CENSUS"]


@pytest.mark.parametrize("name", sorted(LEGACY_PLANTS))
def test_the_audits_plants_are_caught(name):
    assert _plant_findings(LEGACY_PLANTS[name]), f"plant {name} not caught"


_NO_STORE = pytest.mark.xfail(not (SRC / "plan_store.py").exists(), strict=True,
                              reason="names plan_store, which does not exist yet")


@pytest.mark.parametrize("name", [
    pytest.param(n, marks=_NO_STORE) if "plan_store" in code else n
    for n, code in sorted(STORE_PLANTS.items())])
def test_the_plants_against_the_store_are_caught(name):
    assert _plant_findings(STORE_PLANTS[name]), f"plant {name} not caught"


def test_the_negative_control_is_clean():
    assert _plant_findings(NEGATIVE_CONTROL) == []
