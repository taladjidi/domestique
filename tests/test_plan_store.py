"""plan_store, the plan's one owner (contract 10.1, 10.2, 10.6 G1/G2/G6).

Each guard is shown failing on a planted violation. The plants that must
look like product code (G2) live in a module written to tmp_path and
imported from there: the test session exempts only code under tests/.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import plan_store as ps

SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def pid(tmp_path):
    ps.bind("t1", tmp_path / "profiles" / "t1" / "plans")
    return "t1"


def _live(pid) -> Path:
    return ps._paths(pid).live


def _put(pid, plan):
    ps.update(pid, lambda _p: plan, reason="test seed")


def _product_module(tmp_path, code: str, name="zz_plant_mod"):
    """A module outside tests/ -- what the audit hook sees as product code."""
    d = tmp_path / "srcplant"
    d.mkdir(exist_ok=True)
    f = d / f"{name}.py"
    f.write_text(textwrap.dedent(code))
    spec = importlib.util.spec_from_file_location(name, f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── read / update ────────────────────────────────────────────────────────────
def test_read_is_none_without_a_plan(pid):
    assert ps.read(pid) is None


def test_update_writes_and_read_reads_a_fresh_copy(pid):
    _put(pid, {"weeks": [], "goal": {"x": 1}})
    a, b = ps.read(pid), ps.read(pid)
    assert a == {"weeks": [], "goal": {"x": 1}} and a is not b


def test_update_hands_the_stored_plan_and_none_writes_nothing(pid):
    _put(pid, {"n": 1})
    before = _live(pid).stat().st_mtime_ns
    seen = []
    assert ps.update(pid, lambda p: seen.append(p) or None, reason="t") is None
    assert seen == [{"n": 1}] and _live(pid).stat().st_mtime_ns == before


def test_update_needs_a_reason(pid):
    with pytest.raises(ValueError):
        ps.update(pid, lambda p: {"n": 1}, reason="")


def test_a_mutator_exception_writes_nothing(pid):
    _put(pid, {"n": 1})

    def boom(p):
        p["n"] = 2
        raise KeyError("x")
    with pytest.raises(KeyError):
        ps.update(pid, boom, reason="t")
    assert ps.read(pid) == {"n": 1}


@pytest.mark.parametrize("raw", [b'{"weeks": [', b"", b"[]", b"{}", b'"x"'])
def test_a_corrupt_plan_raises_one_error(pid, raw):
    _live(pid).parent.mkdir(parents=True, exist_ok=True)
    _live(pid).write_bytes(raw)
    with pytest.raises(ps.PlanCorrupt):
        ps.read(pid)
    with pytest.raises(ps.PlanCorrupt):
        ps.update(pid, lambda p: {"n": 1}, reason="t")
    assert _live(pid).read_bytes() == raw


def test_generate_replaces_a_corrupt_plan_and_keeps_it_as_a_backup(pid):
    _live(pid).parent.mkdir(parents=True, exist_ok=True)
    _live(pid).write_bytes(b'{"weeks": [')
    handed = []
    ps.update(pid, lambda p: handed.append(p) or {"n": 1}, reason="generate", replace_corrupt=True)
    assert handed == [None] and ps.read(pid) == {"n": 1}
    assert ps._paths(pid).bak(1).read_bytes() == b'{"weeks": ['


def test_backups_rotate_seven_deep(pid):
    for i in range(10):
        _put(pid, {"gen": i})
    p = ps._paths(pid)
    assert json.loads(p.bak(1).read_text()) == {"gen": 8}
    assert json.loads(p.bak(7).read_text()) == {"gen": 2}
    assert not p.bak(8).exists() and not p.tmp.exists() and not p.snap.exists()


def test_an_empty_plan_is_refused(pid):
    with pytest.raises(ValueError):
        ps.update(pid, lambda p: {}, reason="t")


def test_the_identity_is_the_profile_not_a_global(tmp_path):
    ps.bind("a", tmp_path / "a")
    ps.bind("b", tmp_path / "b")
    _put("a", {"who": "a"})
    _put("b", {"who": "b"})
    assert ps.read("a") == {"who": "a"} and ps.read("b") == {"who": "b"}


@pytest.mark.parametrize("bad", [None, "", "../x", "a/b", ".."])
def test_a_profile_id_is_checked(bad):
    with pytest.raises(ps.PlanStoreError):
        ps.read(bad)


def test_the_callback_runs_after_every_write_with_the_profile(pid, monkeypatch):
    got = []
    monkeypatch.setattr(ps, "post_write_callback", got.append)
    _put(pid, {"n": 1})
    ps.update(pid, lambda p: None, reason="t")
    assert got == [pid]


def test_the_store_refuses_the_event_loop(pid):
    async def handler():
        ps.update(pid, lambda p: {"n": 1}, reason="t")
    with pytest.raises(ps.PlanStoreError, match="event loop"):
        asyncio.run(handler())
    assert ps.read(pid) is None


# ── G1: the private writer needs the token ───────────────────────────────────
def test_g1_the_private_writer_refuses_without_the_token(pid):
    with pytest.raises(ps.PlanStoreError, match="G1"):
        ps._commit(ps._paths(pid), {"n": 1})
    assert ps.read(pid) is None


def test_g1_a_write_inside_a_mutator_is_refused(pid):
    _put(pid, {"n": 1})

    def nested(p):
        ps._commit(ps._paths(pid), {**p, "nested": 1})
        return None
    with pytest.raises(ps.PlanStoreError, match="G1"):
        ps.update(pid, nested, reason="t")
    assert ps.read(pid) == {"n": 1}


def test_g1_an_update_inside_a_mutator_is_refused(pid):
    _put(pid, {"n": 1})
    with pytest.raises(ps.PlanStoreError, match="mutator"):
        ps.update(pid, lambda p: ps.update(pid, lambda q: {"n": 3}, reason="in") or p, reason="t")
    assert ps.read(pid) == {"n": 1}


def test_g1_the_token_for_one_profile_does_not_open_another(tmp_path):
    ps.bind("a", tmp_path / "a")
    ps.bind("b", tmp_path / "b")
    _put("a", {"who": "a"})

    def cross(p):
        return p
    with ps._held(ps._paths("a")):
        with pytest.raises(ps.PlanStoreError):
            ps._commit(ps._paths("b"), {"who": "a"})
    assert ps.read("b") is None


# ── G2: the audit hook ───────────────────────────────────────────────────────
G2_PLANTS = {
    "raw_open_write": "def go(p):\n    open(p, 'w').write('{}')\n",
    "path_write_text": "def go(p):\n    p.write_text('{}')\n",
    "raw_read": "def go(p):\n    return open(p).read()\n",
    "os_replace": "import os\ndef go(p):\n    t = p.with_name('x.tmp'); t.write_text('{}'); os.replace(t, p)\n",
    "os_remove": "import os\ndef go(p):\n    os.remove(p)\n",
    "shutil_copy_over": "import shutil\ndef go(p):\n    t = p.parent.parent / 'y.json'; t.write_text('{}'); shutil.copy2(t, p)\n",
    "shutil_copy_backup_out": "import shutil\ndef go(p):\n    shutil.copy2(p.with_name('current_plan.json.bak'), p.parent.parent / 'z')\n",
    "rmtree_profile": "import shutil\ndef go(p):\n    shutil.rmtree(p.parent.parent)\n",
    "move_plans": "import shutil\ndef go(p):\n    shutil.move(str(p.parent), str(p.parent.parent / 'moved'))\n",
    "md_in_plan_dir": "def go(p):\n    (p.parent / 'plan_2026-09-27.md').write_text('# plan')\n",
    "subprocess_cp": "import subprocess\ndef go(p):\n    subprocess.run(['cp', '/dev/null', str(p)])\n",
    "os_open": "import os\ndef go(p):\n    os.close(os.open(p, os.O_WRONLY | os.O_TRUNC))\n",
}


G2_LOOP_PLANT = """
import plan_store as ps
async def go(p):
    with ps._granted(p.parent):
        p.write_text('{"n": 9}')
"""

# Runs in its own interpreter: the hook, once installed, stays for the
# process. It seeds a plan through the store, then runs each plant as a
# module outside tests/ (so nothing is exempt) and reports what happened.
_G2_RIG = """
import asyncio, importlib.util, json, os, sys
from pathlib import Path
sys.path.insert(0, {src!r})
import plan_store as ps
ps.install_guard()
tmp = Path({tmp!r})
plants = json.loads(Path({plants!r}).read_text())
out = {{}}
for name, code in sorted(plants.items()):
    ps.bind("g2", tmp / name / "profiles" / "g2" / "plans")
    ps.update("g2", lambda p: {{"n": 1}}, reason="seed")
    ps.update("g2", lambda p: {{"n": 2}}, reason="seed")
    f = tmp / "mods" / (name + ".py")
    f.parent.mkdir(exist_ok=True)
    f.write_text(code)
    spec = importlib.util.spec_from_file_location("zz_" + name, f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    n0 = len(ps.REFUSALS)
    live = ps._paths("g2").live
    try:
        r = mod.go(live)
        if asyncio.iscoroutine(r):
            asyncio.run(r)
        res = "allowed"
    except ps.PlanStoreError as e:
        res = "refused: " + str(e)[:120]
    except Exception as e:
        res = "error: " + type(e).__name__ + str(e)[:80]
    try:
        intact = ps.read("g2") == {{"n": 2}}
    except Exception:
        intact = False
    out[name] = {{"result": res, "intact": intact, "logged": len(ps.REFUSALS) - n0}}
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def g2(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("g2")
    plants = tmp / "plants.json"
    plants.write_text(json.dumps({**G2_PLANTS, "loop_write": G2_LOOP_PLANT,
                                  "control_store_update": "import plan_store as ps\n"
                                  "def go(p):\n    ps.update('g2', lambda q: {**q, 'm': 1}, reason='x')\n"}))
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    env["DOMESTIQUE_HOME"] = str(tmp / "home")
    r = subprocess.run([sys.executable, "-c", _G2_RIG.format(src=str(SRC), tmp=str(tmp),
                                                             plants=str(plants))],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("name", sorted(G2_PLANTS) + ["loop_write"])
def test_g2_plan_access_outside_the_store_is_refused(g2, name):
    got = g2[name]
    assert got["result"].startswith("refused: G2"), got
    assert got["intact"] and got["logged"] == 1, got


def test_g2_the_store_itself_is_not_refused(g2):
    assert g2["control_store_update"] == {"result": "allowed", "intact": False, "logged": 0}


def test_g2_the_legacy_root_and_profile_plan_dirs_are_plan_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("DOMESTIQUE_HOME", str(home))
    for p in (home / "plans" / "plan_x.md", home / "profiles" / "r" / "plans" / "anything"):
        assert ps._plan_dir_of(p) == str(p.parent)
    assert ps._plan_dir_of(home / "profiles" / "r" / "athlete.json") is None
    assert ps._plan_dir_of(home / "profiles" / "r", tree=True) is None
    (home / "profiles" / "r" / "plans").mkdir(parents=True)
    assert ps._plan_dir_of(home / "profiles" / "r", tree=True) == str(home / "profiles" / "r" / "plans")


def test_g2_production_exempts_nothing():
    """The exemption for test files exists only in a session that asked for
    it: a plain interpreter that imports the app has none."""
    code = ("import sys; sys.path.insert(0, %r); import plan_store; "
            "plan_store.install_guard(); print(plan_store._TEST_ROOTS, plan_store._HOOKED)" % str(SRC))
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, timeout=60).stdout.strip()
    assert out == "() True"


# ── G6: one writer across processes ──────────────────────────────────────────
_COUNTER = """
import sys, time
sys.path.insert(0, {src!r})
import plan_store as ps
ps.bind("c", {d!r})
for i in range({n}):
    def m(p):
        p = p or {{"n": 0}}
        n = p["n"]
        time.sleep(0.002)       # widen the read -> write window
        return {{"n": n + 1}}
    ps.update("c", m, reason="count")
"""


def _counters(tmp_path, n=60, procs=3):
    d = str(tmp_path / "plans")
    code = _COUNTER.format(src=str(SRC), d=d, n=n)
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    ps_ = [subprocess.Popen([sys.executable, "-c", code], env=env) for _ in range(procs)]
    for p in ps_:
        assert p.wait(timeout=120) == 0
    return json.loads((Path(d) / "current_plan.json").read_text())["n"]


def test_g6_two_processes_never_interleave(tmp_path):
    assert _counters(tmp_path) == 180


def test_g6_the_flock_is_what_holds(tmp_path, monkeypatch):
    """Control: without the file lock, three processes interleave -- a lost
    update, or a write that loses its temp file to another process's."""
    src = tmp_path / "nolock"
    shutil.copytree(SRC, src, ignore=shutil.ignore_patterns("__pycache__", "workouts",
                                                             "static", "templates", "profiles"))
    f = src / "plan_store.py"
    f.write_text(f.read_text().replace("                    fcntl.flock(fd, fcntl.LOCK_EX)",
                                       "                    pass"))
    d = str(tmp_path / "plans")
    code = _COUNTER.format(src=str(src), d=d, n=60)
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stderr=subprocess.DEVNULL)
             for _ in range(3)]
    rcs = [p.wait(timeout=120) for p in procs]
    n = json.loads((Path(d) / "current_plan.json").read_text())["n"]
    assert any(rcs) or n < 180, (rcs, n)


def test_g6_threads_in_one_process_never_interleave(pid):
    _put(pid, {"n": 0})

    def work():
        for _ in range(50):
            ps.update(pid, lambda p: {"n": p["n"] + 1}, reason="count")
    ts = [threading.Thread(target=work) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert ps.read(pid) == {"n": 200}


# ── named operations ─────────────────────────────────────────────────────────
def test_restore_from_backup_puts_the_newest_parseable_backup_back(pid):
    _put(pid, {"gen": 1})
    _put(pid, {"gen": 2})
    _put(pid, {"gen": 3})
    p = ps._paths(pid)
    p.bak(1).write_text("{broken")
    p.live.write_bytes(b"")
    assert ps.restore_from_backup(pid) == "current_plan.json.bak2"
    assert ps.read(pid) == {"gen": 1}
    assert p.bak(1).read_text() == "{broken"      # the chain is not shifted


def test_restore_leaves_a_live_plan_alone(pid):
    _put(pid, {"gen": 1})
    _put(pid, {"gen": 2})
    assert ps.restore_from_backup(pid) is None and ps.read(pid) == {"gen": 2}


def test_drop_removes_the_profiles_plans(pid):
    _put(pid, {"n": 1})
    _put(pid, {"n": 2})
    ps.drop(pid)
    assert not ps._paths(pid).dir.exists()


def test_copy_legacy_plans(tmp_path, pid):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "current_plan.json").write_text('{"n": 1}')
    (legacy / "current_plan.json.bak").write_text('{"n": 0}')
    assert ps.copy_legacy_plans(pid, legacy) == 2
    assert ps.read(pid) == {"n": 1}


def test_g2_is_installed_in_the_test_session_and_the_app(tmp_path):
    """The hook the app installs on import is live here: product code (a
    module outside tests/) writing a plan file is refused in-process."""
    import app  # noqa: F401  -- installs it
    assert ps._HOOKED
    ps.bind("inproc", tmp_path / "plans")
    _put("inproc", {"n": 1})
    mod = _product_module(tmp_path, "def go(p):\n    p.write_text('{}')\n", name="zz_inproc")
    with pytest.raises(ps.PlanStoreError, match="G2"):
        mod.go(_live("inproc"))
    assert ps.read("inproc") == {"n": 1}
