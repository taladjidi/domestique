"""The test suite's plan seam (contract 10.3): where the ACTIVE profile's
plans live, bound through plan_store (the one owner) -- not through a global
the app reads (training_planner.PLAN_DIR / app._plan_dir until the one-plan
round). conftest restores the bindings after every test."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import plan_store


def active() -> str:
    from profile_manager import ProfileManager
    return ProfileManager.get().active_id


def bind_active(plan_dir) -> Path:
    """Keep the active profile's plans in ``plan_dir``."""
    plan_store.bind(active(), plan_dir)
    return Path(plan_dir)


def unbind_active() -> None:
    plan_store._dirs.pop(active(), None)


def active_plan_dir() -> Path:
    """The dir the store reads the active profile's plan from."""
    return plan_store._paths(active()).dir


def patch_plan_dir(plan_dir):
    """bind_active as a patch (start/stop, or a context manager)."""
    return mock.patch.dict(plan_store._dirs, {active(): Path(plan_dir)})
