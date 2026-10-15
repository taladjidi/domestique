"""#32 — every plan read-modify-write in app.py runs under the plan lock.

17 of 24 places that read the plan, changed it and wrote it back did so
without ``tp.plan_write_lock()``. Two of them overlapping is a lost update:
the second writer saves the copy it read before the first one wrote, and the
first change is gone without a trace. A background writer (the sync's
adaptation today, intervals.icu read-back in #33) makes that likely.

Two checks: a structural one (a new unlocked writer fails here, not in a
rider's plan), and the race itself on one endpoint.
"""
from __future__ import annotations

import ast
import json
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import training_planner as tp  # noqa: E402
from test_plan_api import PlanApiTestBase  # noqa: E402

APP = Path(__file__).resolve().parent.parent / "src" / "app.py"

# Our line (contracts 4.1 / 4.2) keeps one read-modify-write, the plan store's
# tp.update_plan: it holds the plan lock across read -> mutate -> write, and
# async handlers reach it through run_in_threadpool. Upstream's #32 wraps each
# endpoint in `with tp.plan_write_lock()` / `async with plan_write_lock_async()`
# and checks that text; these checks assert the same guarantee against the
# store: no plan write outside the lock, and no lock taken on the event loop.

# Helpers that write a plan their CALLER read under the lock (the lock is an
# RLock, so the caller holds it across the call). Each entry names the caller.
_WRITES_UNDER_CALLER_LOCK: dict = {}


def _is_plan_lock(node: ast.AST) -> bool:
    return isinstance(node, ast.With) and any(
        isinstance(i.context_expr, ast.Call)
        and isinstance(i.context_expr.func, ast.Attribute)
        and i.context_expr.func.attr == "plan_write_lock"
        for i in node.items)


def _calls(tree, attr):
    return [c for c in ast.walk(tree) if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute) and c.func.attr == attr]


def test_every_plan_write_in_app_is_under_the_plan_lock():
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    unlocked = []
    for call in _calls(tree, "atomic_write_plan"):
        node, locked, fn = call, False, None
        while node in parents:
            node = parents[node]
            locked = locked or _is_plan_lock(node)
            if fn is None and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = node.name
        if not locked and fn not in _WRITES_UNDER_CALLER_LOCK:
            unlocked.append(f"{fn} (line {call.lineno})")
    assert unlocked == [], "plan writes outside the plan store / plan lock: " + ", ".join(unlocked)


def test_every_read_modify_write_goes_through_the_store_off_the_event_loop():
    """The read-modify-writes are update_plan calls (not vacuous: the plan
    endpoints are all there); an async handler hands the store to
    run_in_threadpool and never calls it on the loop (contract 4.2)."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    handed = [
        c for c in ast.walk(tree) if isinstance(c, ast.Call)
        and getattr(c.func, "id", getattr(c.func, "attr", None)) == "run_in_threadpool"
        and c.args and isinstance(c.args[0], ast.Attribute) and c.args[0].attr == "update_plan"]
    direct = _calls(tree, "update_plan")
    assert len(handed) + len(direct) >= 20, "the plan writers no longer use the store?"
    on_loop = []
    for call in direct:
        node = call
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                if isinstance(node, ast.AsyncFunctionDef):
                    on_loop.append(f"{node.name} (line {call.lineno})")
                break
    assert on_loop == [], "update_plan called on the event loop: " + ", ".join(on_loop)


def test_no_await_inside_a_plan_lock():
    """A threading RLock held across an await would let another coroutine on
    the same thread re-enter it; keep every locked block await-free."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    bad = [n.lineno for n in ast.walk(tree) if _is_plan_lock(n)
           and any(isinstance(m, ast.Await) for m in ast.walk(n))]
    assert bad == []


class TestNoLostUpdate(PlanApiTestBase):
    def test_a_dismiss_waits_for_a_background_writer_and_keeps_its_change(self):
        """A background writer reads the plan, takes a while, and writes a
        change. A rider's dismiss arriving meanwhile must wait for it and then
        apply on top: both changes survive. Before #32 the dismiss read and
        wrote in between, and the background writer's stale copy erased it."""
        tue = (self._monday + timedelta(days=1)).isoformat()
        json_path = self._tmp / "current_plan.json"
        started = threading.Event()

        def background_writer():
            with tp.plan_write_lock():
                plan = json.loads(json_path.read_text())
                started.set()
                time.sleep(0.4)                       # e.g. a reforecast
                plan["background_marker"] = True
                tp.atomic_write_plan(json_path, plan)

        t = threading.Thread(target=background_writer)
        t.start()
        assert started.wait(5)
        r = self.client.post("/api/plan/dismiss-session", json={"date": tue})
        t.join(5)
        self.assertEqual(r.status_code, 200, r.text)
        plan = json.loads(json_path.read_text())
        self.assertTrue(plan.get("background_marker"), "background change lost")
        tue_s = next(s for w in plan["weeks"] for s in w["sessions"] if s["day"] == tue)
        self.assertEqual(tue_s.get("status"), "dismissed", "rider's dismiss lost")
