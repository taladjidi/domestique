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

# The worklist: every finding at 433c6067, keyed "KIND module:function  what".
ALLOWED: Counter = Counter({
    "ACCESS app:_adopt_legacy_root_plan  'current_plan.json'": 3,
    "ACCESS app:_adopt_legacy_root_plan  'current_plan.json.adopted-from-root'": 1,
    "ACCESS app:_adopt_legacy_root_plan  'current_plan.json.bak'": 2,
    "ACCESS app:_adopt_legacy_root_plan  'current_plan.json.pre-v3'": 1,
    "ACCESS app:_adopt_legacy_root_plan  path / 'current_plan.json'": 3,
    "ACCESS app:_adopt_legacy_root_plan  path / 'current_plan.json.adopted-from-root'": 1,
    "ACCESS app:_adopt_legacy_root_plan  path / 'current_plan.json.pre-v3'": 1,
    "ACCESS app:_api_today_session_impl  'current_plan.json'": 1,
    'ACCESS app:_api_today_session_impl  _plan_dir()': 1,
    "ACCESS app:_api_today_session_impl  path / 'current_plan.json'": 1,
    "ACCESS app:_detect_plan_load_alert  'current_plan.json'": 1,
    'ACCESS app:_detect_plan_load_alert  _plan_dir()': 1,
    "ACCESS app:_detect_plan_load_alert  path / 'current_plan.json'": 1,
    "ACCESS app:_enrich_plan_for_response  'current_plan.json'": 1,
    'ACCESS app:_enrich_plan_for_response  _plan_dir()': 1,
    "ACCESS app:_enrich_plan_for_response  path / 'current_plan.json'": 1,
    "ACCESS app:_heal_plan_on_disk  'current_plan.json'": 1,
    'ACCESS app:_heal_plan_on_disk  _plan_dir()': 1,
    "ACCESS app:_heal_plan_on_disk  path / 'current_plan.json'": 1,
    "ACCESS app:_icu_push_schedule_debounced  'current_plan.json'": 1,
    "ACCESS app:_maybe_auto_reforecast  'current_plan.json'": 1,
    'ACCESS app:_maybe_auto_reforecast  _plan_dir()': 1,
    "ACCESS app:_maybe_auto_reforecast  path / 'current_plan.json'": 1,
    "ACCESS app:_next_ftp_test_slot  'current_plan.json'": 1,
    'ACCESS app:_next_ftp_test_slot  _plan_dir()': 1,
    "ACCESS app:_next_ftp_test_slot  path / 'current_plan.json'": 1,
    "ACCESS app:_plan_file_mtime_ns  'current_plan.json'": 1,
    'ACCESS app:_plan_file_mtime_ns  _plan_dir()': 1,
    "ACCESS app:_plan_file_mtime_ns  path / 'current_plan.json'": 1,
    "ACCESS app:_read_stored_plan  'current_plan.json'": 1,
    'ACCESS app:_read_stored_plan  _plan_dir()': 1,
    "ACCESS app:_read_stored_plan  path / 'current_plan.json'": 1,
    "ACCESS app:api_calendar  'current_plan.json'": 1,
    'ACCESS app:api_calendar  _plan_dir()': 1,
    "ACCESS app:api_calendar  path / 'current_plan.json'": 1,
    "ACCESS app:api_continuous_deload_revert  'current_plan.json'": 1,
    'ACCESS app:api_continuous_deload_revert  _plan_dir()': 1,
    "ACCESS app:api_continuous_deload_revert  path / 'current_plan.json'": 1,
    "ACCESS app:api_diag_health  'current_plan.json'": 1,
    'ACCESS app:api_diag_health  _plan_dir()': 1,
    "ACCESS app:api_diag_health  path / 'current_plan.json'": 1,
    "ACCESS app:api_event_projection  'current_plan.json'": 1,
    'ACCESS app:api_event_projection  _plan_dir()': 1,
    "ACCESS app:api_event_projection  path / 'current_plan.json'": 1,
    "ACCESS app:api_mark_unavailable  'current_plan.json'": 1,
    'ACCESS app:api_mark_unavailable  _plan_dir()': 1,
    "ACCESS app:api_mark_unavailable  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan  'current_plan.json'": 1,
    'ACCESS app:api_plan  _plan_dir()': 3,
    "ACCESS app:api_plan  glob('*.md')": 2,
    "ACCESS app:api_plan  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_accept_redraw  'current_plan.json'": 1,
    'ACCESS app:api_plan_accept_redraw  _plan_dir()': 1,
    "ACCESS app:api_plan_accept_redraw  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_add_race  'current_plan.json'": 1,
    'ACCESS app:api_plan_add_race  _plan_dir()': 1,
    "ACCESS app:api_plan_add_race  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_auto_adjust  'current_plan.json'": 1,
    'ACCESS app:api_plan_auto_adjust  _plan_dir()': 1,
    "ACCESS app:api_plan_auto_adjust  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_auto_recalc  'current_plan.json'": 1,
    'ACCESS app:api_plan_auto_recalc  _plan_dir()': 1,
    "ACCESS app:api_plan_auto_recalc  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_daily_adapt  'current_plan.json'": 1,
    'ACCESS app:api_plan_daily_adapt  _plan_dir()': 1,
    "ACCESS app:api_plan_daily_adapt  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_dismiss_session  'current_plan.json'": 1,
    'ACCESS app:api_plan_dismiss_session  _plan_dir()': 1,
    "ACCESS app:api_plan_dismiss_session  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_ftp_test_type  'current_plan.json'": 1,
    'ACCESS app:api_plan_ftp_test_type  _plan_dir()': 1,
    "ACCESS app:api_plan_ftp_test_type  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_generate  'current_plan.json'": 1,
    'ACCESS app:api_plan_generate  _plan_dir()': 1,
    "ACCESS app:api_plan_generate  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_move_session  'current_plan.json'": 1,
    'ACCESS app:api_plan_move_session  _plan_dir()': 1,
    "ACCESS app:api_plan_move_session  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_preview_redraw  'current_plan.json'": 1,
    'ACCESS app:api_plan_preview_redraw  _plan_dir()': 1,
    "ACCESS app:api_plan_preview_redraw  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_re_draw  'current_plan.json'": 2,
    'ACCESS app:api_plan_re_draw  _plan_dir()': 2,
    "ACCESS app:api_plan_re_draw  path / 'current_plan.json'": 2,
    "ACCESS app:api_plan_reforecast  'current_plan.json'": 1,
    'ACCESS app:api_plan_reforecast  _plan_dir()': 1,
    "ACCESS app:api_plan_reforecast  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_regenerate_dynamic  'current_plan.json'": 1,
    'ACCESS app:api_plan_regenerate_dynamic  _plan_dir()': 1,
    "ACCESS app:api_plan_regenerate_dynamic  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_rematch  'current_plan.json'": 1,
    'ACCESS app:api_plan_rematch  _plan_dir()': 1,
    "ACCESS app:api_plan_rematch  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_swap_type  'current_plan.json'": 1,
    'ACCESS app:api_plan_swap_type  _plan_dir()': 1,
    "ACCESS app:api_plan_swap_type  path / 'current_plan.json'": 1,
    "ACCESS app:api_plan_update  'current_plan.json'": 1,
    'ACCESS app:api_plan_update  _plan_dir()': 1,
    "ACCESS app:api_plan_update  path / 'current_plan.json'": 1,
    "ACCESS app:api_programme_summary  'current_plan.json'": 1,
    'ACCESS app:api_programme_summary  _plan_dir()': 1,
    "ACCESS app:api_programme_summary  path / 'current_plan.json'": 1,
    "ACCESS app:api_programme_summary_png  'current_plan.json'": 1,
    'ACCESS app:api_programme_summary_png  _plan_dir()': 1,
    "ACCESS app:api_programme_summary_png  path / 'current_plan.json'": 1,
    "ACCESS app:api_readiness_apply_tier_down  'current_plan.json'": 1,
    'ACCESS app:api_readiness_apply_tier_down  _plan_dir()': 1,
    "ACCESS app:api_readiness_apply_tier_down  path / 'current_plan.json'": 1,
    "ACCESS app:api_readiness_revert_cap  'current_plan.json'": 1,
    'ACCESS app:api_readiness_revert_cap  _plan_dir()': 1,
    "ACCESS app:api_readiness_revert_cap  path / 'current_plan.json'": 1,
    "ACCESS app:api_save_availability  'current_plan.json'": 1,
    'ACCESS app:api_save_availability  _plan_dir()': 1,
    "ACCESS app:api_save_availability  path / 'current_plan.json'": 1,
    "ACCESS app:api_today_session_persist  'current_plan.json'": 1,
    'ACCESS app:api_today_session_persist  _plan_dir()': 1,
    "ACCESS app:api_today_session_persist  path / 'current_plan.json'": 1,
    "ACCESS app:lifespan  'current_plan.json'": 3,
    'ACCESS app:lifespan  _DEFAULT_PLAN_DIR': 1,
    'ACCESS app:lifespan  _plan_dir()': 3,
    "ACCESS app:lifespan  path / 'current_plan.json'": 3,
    "ACCESS icu_calendar_push:_load_plan  'current_plan.json'": 1,
    "ACCESS icu_calendar_push:_load_plan  path / 'current_plan.json'": 1,
    "ACCESS icu_calendar_push:_load_plan  path / 'plans'": 1,
    "ACCESS migrate_profiles:migrate_to_profiles  path / 'plans'": 3,
    'ACCESS paths:<module>  _DEFAULT_PLAN_DIR': 1,
    "ACCESS paths:<module>  path / 'plans'": 1,
    'ACCESS paths:_plan_dir  _DEFAULT_PLAN_DIR': 1,
    "ACCESS power_curve:_planned_ftp_test_hint  'current_plan.json'": 1,
    'ACCESS power_curve:_planned_ftp_test_hint  PLAN_DIR': 1,
    "ACCESS power_curve:_planned_ftp_test_hint  path / 'current_plan.json'": 1,
    'ACCESS profile_manager:ProfileManager.apply_training_dirs  PLAN_DIR': 1,
    "ACCESS profile_manager:ProfileManager.create_profile  path / 'plans'": 1,
    "ACCESS profile_manager:ProfileManager.plan_dir  path / 'plans'": 1,
    'ACCESS training_planner:<module>  PLAN_DIR': 1,
    "ACCESS training_planner:<module>  path / 'plans'": 1,
    'ACCESS training_planner:export_plan_md  PLAN_DIR': 1,
    'DOOR app:_generate_into_store  update_plan': 1,
    'DOOR app:_maybe_auto_reforecast  update_plan': 2,
    'DOOR app:_maybe_heal_plan_files  update_plan': 1,
    'DOOR app:api_continuous_deload_revert  update_plan (offloaded)': 1,
    'DOOR app:api_mark_unavailable  update_plan (offloaded)': 1,
    'DOOR app:api_plan_accept_redraw  update_plan (offloaded)': 1,
    'DOOR app:api_plan_add_race  update_plan (offloaded)': 1,
    'DOOR app:api_plan_auto_adjust  update_plan (offloaded)': 1,
    'DOOR app:api_plan_auto_recalc  update_plan': 1,
    'DOOR app:api_plan_dismiss_session  update_plan (offloaded)': 1,
    'DOOR app:api_plan_ftp_test_type  update_plan (offloaded)': 1,
    'DOOR app:api_plan_move_session  update_plan (offloaded)': 1,
    'DOOR app:api_plan_re_draw  update_plan (offloaded)': 1,
    'DOOR app:api_plan_reforecast  update_plan (offloaded)': 1,
    'DOOR app:api_plan_regenerate_dynamic  update_plan (offloaded)': 1,
    'DOOR app:api_plan_rematch  update_plan (offloaded)': 1,
    'DOOR app:api_plan_swap_type  update_plan (offloaded)': 1,
    'DOOR app:api_plan_update  update_plan (offloaded)': 1,
    'DOOR app:api_readiness_apply_tier_down  update_plan (offloaded)': 1,
    'DOOR app:api_readiness_revert_cap  update_plan (offloaded)': 1,
    'DOOR app:api_save_availability  update_plan (offloaded)': 1,
    'DOOR app:api_today_session_persist  update_plan (offloaded)': 1,
    'DOOR training_planner:rewrite_stale_plan_classifications  update_plan': 1,
    'LOOP app:lifespan  lifespan -> app:_maybe_restore_plan_from_backup': 1,
    'OWNER app:_adopt_legacy_root_plan  _rotate_plan_backups': 1,
    'OWNER app:_adopt_legacy_root_plan  plan_write_lock': 1,
    'OWNER app:_adopt_legacy_root_plan  raw Path.mkdir': 1,
    'OWNER app:_adopt_legacy_root_plan  raw Path.replace': 2,
    'OWNER app:_adopt_legacy_root_plan  raw Path.write_text': 2,
    'OWNER app:_adopt_legacy_root_plan  raw shutil.copy2': 2,
    'OWNER app:_generate_into_store  atomic_write_plan': 1,
    'OWNER app:_generate_into_store  plan_write_lock': 1,
    'OWNER app:_maybe_restore_plan_from_backup  raw Path.replace': 1,
    "OWNER app:_maybe_restore_plan_from_backup  raw open(..., 'w')": 1,
    'OWNER migrate_profiles:migrate_to_profiles  raw Path.mkdir': 1,
    'OWNER migrate_profiles:migrate_to_profiles  raw shutil.copy2': 1,
    'OWNER profile_manager:ProfileManager.create_profile  raw Path.mkdir': 1,
    'OWNER profile_manager:ProfileManager.plan_dir  raw Path.mkdir': 1,
    'OWNER training_planner:<module>  _plan_write_lock (raw lock)': 1,
    'OWNER training_planner:_rewrite_stale_sessions  raw shutil.copy2': 1,
    'OWNER training_planner:export_plan_md  raw Path.mkdir': 1,
    "OWNER training_planner:export_plan_md  raw open(..., 'w')": 1,
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
