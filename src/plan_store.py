"""The plan's one owner (contract 10.1-10.3, 10.6).

The plan is one file per profile, ``profiles/<id>/plans/current_plan.json``,
plus its rotated backups. This module is the only one that names that path
or touches those files:

  read(profile_id)                      the plan, None when there is none;
                                        PlanCorrupt when it does not parse
  update(profile_id, mutate, reason=)   the one read-modify-write
  version(profile_id)                   a change stamp, for caches

and the named store operations the app's boot and profile lifecycle need,
which write through the same private writer: restore_from_backup,
adopt_legacy_root, copy_legacy_plans, drop.

Every call names the profile. The id is captured when a request or a job
starts, so a profile switch landing mid-operation cannot turn one rider's
writer onto another rider's file; nothing here reads "the active profile".

Guards, each of which fails on a planted violation (tests/test_plan_store.py):
  G1  _commit, the private writer, refuses to run unless the operation that
      called it granted the token for that plan dir (a ContextVar). A mutator
      never holds it: a write from inside ``mutate`` is refused.
  G2  an audit hook (sys.addaudithook, installed when this module is first
      imported -- by the app at start and by the test session) refuses any
      open, write, rename, replace, remove, copy, move, mkdir or rmtree that
      lands on a plan path without the token, any store write on a thread
      running an event loop, and any subprocess whose arguments name a plan
      path. A refusal raises PlanStoreError and is kept in REFUSALS, so one
      swallowed by a best-effort caller is still seen.
  G6  update and every named operation hold fcntl.flock on the plan dir's
      lock file across the whole read-modify-write, so a second app instance
      or a CLI cannot interleave. The lock is on current_plan.lock, not on
      current_plan.json itself: the write replaces the plan file by rename,
      and a lock on the old inode would not exclude a process that opened
      the new one.

After every write -- routes, boot restore, legacy adoption, migration --
``post_write_callback(profile_id)`` runs (the app arms its calendar push).
"""
from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import os
import shutil
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from user_home import domestique_home

try:
    import fcntl
except ImportError:          # ponytail: no flock off POSIX; the in-process lock still holds
    fcntl = None

log = logging.getLogger("plan_store")

PLAN = "current_plan.json"
BACKUP_DEPTH = 7             # .bak through .bak7


class PlanCorrupt(Exception):
    """The stored plan exists but is not a non-empty JSON object."""

    code = "E_PLAN_PARSE_CORRUPT"


class PlanStoreError(PermissionError):
    """A plan access refused by the store's guards."""


REFUSALS: list[str] = []

# ── identity: a profile id -> its plans dir ──────────────────────────────────
_profiles_root: Path | None = None       # the home's profiles/ (profile_manager sets it)
_dirs: dict[str, Path] = {}              # explicit bindings (tests point a profile at a tmp dir)


def use_profiles_root(root) -> None:
    """Where profiles live: profile_manager says so when it opens a home."""
    global _profiles_root
    _profiles_root = Path(root)


def bind(profile_id: str, plan_dir) -> None:
    """Keep ``profile_id``'s plans in ``plan_dir`` (tests; a relocated home)."""
    _dirs[_check_id(profile_id)] = Path(plan_dir)


def _check_id(profile_id) -> str:
    if not isinstance(profile_id, str) or not profile_id or "/" in profile_id \
            or "\\" in profile_id or profile_id in (".", ".."):
        raise PlanStoreError(f"not a profile id: {profile_id!r}")
    return profile_id


def _legacy_root() -> Path:
    """The single-user plans dir of a pre-profiles home (read by adoption)."""
    return domestique_home() / "plans"


class _Paths:
    __slots__ = ("pid", "dir", "live", "tmp", "snap", "lock")

    def __init__(self, pid: "str | None", d: Path):
        self.pid, self.dir = pid, d
        self.live = d / PLAN
        self.tmp = d / "current_plan.tmp"
        self.snap = d / (PLAN + ".bak.new")
        self.lock = d / "current_plan.lock"

    def bak(self, n: int) -> Path:
        return self.dir / (PLAN + (".bak" if n == 1 else f".bak{n}"))


def _paths(profile_id: str) -> _Paths:
    pid = _check_id(profile_id)
    d = _dirs.get(pid)
    if d is None:
        root = _profiles_root if _profiles_root is not None else domestique_home() / "profiles"
        d = root / pid / "plans"
    return _Paths(pid, d)


# ── G1: the token ────────────────────────────────────────────────────────────
_TOKEN: contextvars.ContextVar = contextvars.ContextVar("plan_store_token", default=frozenset())
_IN_MUTATE: contextvars.ContextVar = contextvars.ContextVar("plan_store_in_mutate", default=False)
_LOCK = threading.RLock()


def _key(p) -> str:
    return os.fsdecode(os.path.abspath(os.fspath(p)))


@contextmanager
def _granted(*dirs: Path):
    tok = _TOKEN.set(_TOKEN.get() | {_key(d) for d in dirs})
    try:
        yield
    finally:
        _TOKEN.reset(tok)


@contextmanager
def _revoked():
    tok = _TOKEN.set(frozenset())
    mt = _IN_MUTATE.set(True)
    try:
        yield
    finally:
        _IN_MUTATE.reset(mt)
        _TOKEN.reset(tok)


def _require_token(paths: _Paths) -> None:
    if _key(paths.dir) not in _TOKEN.get():
        _refuse(f"G1: plan write to {paths.dir} without the store's token "
                f"(called by {_caller()})")


def _refuse(msg: str):
    REFUSALS.append(msg)
    log.error("E_PLAN_STORE_REFUSED %s", msg)
    raise PlanStoreError(msg)


def _caller() -> str:
    f = sys._getframe(1)
    while f is not None and (f.f_code.co_filename == __file__
                             or f.f_code.co_filename.startswith(_STDLIB)):
        f = f.f_back
    return f"{f.f_code.co_name} ({Path(f.f_code.co_filename).name}:{f.f_lineno})" if f else "?"


def _refuse_on_event_loop() -> None:
    """Contract 4.2: waiting on the plan lock from a thread that runs an
    event loop stalls every request. Async code calls the store through
    run_in_threadpool / asyncio.to_thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    f, handler = sys._getframe(1), None
    while f is not None:
        if f.f_code.co_flags & (inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR):
            handler = f
            break
        f = f.f_back
    where = _caller() + (f" in async {handler.f_code.co_name}" if handler else "")
    _refuse(f"the plan store was entered on the event loop by {where}: call it "
            "through run_in_threadpool (contract 4.2)")


# ── G6: one writer across processes ──────────────────────────────────────────
@contextmanager
def _held(*paths_list: _Paths):
    """The in-process lock and an exclusive flock per plan dir, with the
    token for those dirs granted (so the lock files may be opened)."""
    _refuse_on_event_loop()
    if _IN_MUTATE.get():
        _refuse(f"G1: a plan store call from inside a mutator ({_caller()})")
    with _LOCK, _granted(*(p.dir for p in paths_list)):
        fds = []
        try:
            for p in sorted(paths_list, key=lambda p: _key(p.dir)):
                p.dir.mkdir(parents=True, exist_ok=True)
                fd = os.open(p.lock, os.O_RDWR | os.O_CREAT, 0o644)
                fds.append(fd)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            for fd in reversed(fds):
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


# ── reading ──────────────────────────────────────────────────────────────────
def _load(path: Path) -> "dict | None":
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return None
    try:
        plan = json.loads(text)
    except ValueError as e:
        raise PlanCorrupt(f"{path.name}: {e}") from None
    if not isinstance(plan, dict) or not plan:
        raise PlanCorrupt(f"{path.name}: not a non-empty JSON object")
    return plan


def read(profile_id: str) -> "dict | None":
    """The stored plan of ``profile_id``: a fresh dict, None when there is
    none, PlanCorrupt when the file does not parse. Never waits on a writer
    (the write replaces the file by rename, so a read sees a whole plan)."""
    paths = _paths(profile_id)
    with _granted(paths.dir):
        return _load(paths.live)


def version(profile_id: str) -> "int | None":
    """The stored plan's modification stamp (ns), None when there is none."""
    try:
        return _paths(profile_id).live.stat().st_mtime_ns
    except OSError:
        return None


# ── the private writer ───────────────────────────────────────────────────────
def _rotate(paths: _Paths, snapshot: "Path | None") -> None:
    """Shift .bak6->.bak7 ... .bak->.bak2, then the previous generation ->
    .bak. Best-effort: a failed step is logged and the write stands."""
    oldest = paths.bak(BACKUP_DEPTH)
    if oldest.exists():
        try:
            oldest.unlink()
        except OSError as e:
            log.debug(f"plan backups: drop oldest failed: {e}")
    for n in range(BACKUP_DEPTH - 1, 0, -1):
        src, dst = paths.bak(n), paths.bak(n + 1)
        if src.exists():
            try:
                src.replace(dst)
            except OSError as e:
                log.debug(f"plan backups: shift {src.name}->{dst.name} failed: {e}")
    try:
        if snapshot is not None:
            snapshot.replace(paths.bak(1))
        else:
            shutil.copy2(paths.live, paths.bak(1))
    except OSError as e:
        log.debug(f"plan backups: previous generation -> .bak failed: {e}")


def _commit(paths: _Paths, plan: dict, *, rotate: bool = True) -> None:
    """Write ``plan`` as the live file: tmp + fsync, the previous generation
    copied aside, atomic rename, and only then the backup chain shifted (a
    write that fails anywhere up to the rename leaves the live file and the
    chain untouched, contract 3.3). Refuses an empty or non-dict plan."""
    _require_token(paths)
    if not isinstance(plan, dict) or not plan:
        raise ValueError(f"plan_store: refusing to write an empty / non-dict plan "
                         f"(got {type(plan).__name__})")
    paths.dir.mkdir(parents=True, exist_ok=True)
    f = open(paths.tmp, "w", encoding="utf-8")
    try:
        with f:
            json.dump(plan, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        paths.tmp.unlink(missing_ok=True)
        raise
    snap = None
    if rotate and paths.live.exists():
        snap = paths.snap
        try:
            shutil.copy2(paths.live, snap)
        except OSError as e:
            log.debug(f"plan write: snapshot of the live plan failed: {e}")
            snap = None
    try:
        paths.tmp.replace(paths.live)
    except BaseException:
        paths.tmp.unlink(missing_ok=True)
        if snap is not None:
            snap.unlink(missing_ok=True)
        raise
    if snap is not None:
        _rotate(paths, snap)


post_write_callback = None


def _written(profile_id: str, reason: str) -> None:
    log.info("EVENT=plan_written profile=%s reason=%s", profile_id, reason)
    cb = post_write_callback
    if cb is not None:
        try:
            cb(profile_id)
        except Exception as e:  # noqa: BLE001 -- a broken callback never breaks a write
            log.debug(f"plan post-write callback failed: {e}")


# ── the one read-modify-write ────────────────────────────────────────────────
def update(profile_id: str, mutate, *, reason: str, replace_corrupt: bool = False):
    """The plan's one read-modify-write.

    Holds the plan lock (in process and across processes) across read ->
    ``mutate(plan)`` -> write, so no writer can write a stale copy over
    another's change. ``mutate`` receives the plan as stored now (None when
    there is none) and returns the dict to write -- the same one mutated in
    place, or a new one -- or None to write nothing; anything a caller needs
    back travels by closure. An exception from ``mutate`` writes nothing
    and propagates. Returns what was written, or None.

    A plan that does not parse raises PlanCorrupt, unless the caller replaces
    the plan whatever it held (``replace_corrupt=True``: generate), when
    ``mutate`` receives None and the unreadable file becomes the newest
    backup.

    Blocking: an ``async def`` calls it through run_in_threadpool; it
    refuses the event loop's thread. Fetch plan-independent inputs before
    the call: whatever runs inside ``mutate`` holds every other writer.
    """
    if not reason:
        raise ValueError("plan_store.update: say why (reason=)")
    paths = _paths(profile_id)
    with _held(paths):
        try:
            plan = _load(paths.live)
        except PlanCorrupt:
            if not replace_corrupt:
                raise
            log.warning("plan_store: %s unreadable -- %s replaces it", paths.live, reason)
            plan = None
        with _revoked():
            new = mutate(plan)
        if new is None:
            return None
        _commit(paths, new)
    _written(profile_id, reason)
    return new


# ── named store operations (boot, migration, profile lifecycle) ──────────────
def restore_from_backup(profile_id: str) -> "str | None":
    """Boot: when the live plan is missing or empty and a .bak* holds a
    plan, put the newest such plan back (the chain is not shifted: the
    backups restored FROM stay as they are). Returns the backup's name."""
    paths = _paths(profile_id)
    if not paths.dir.exists():
        return None
    with _held(paths):
        try:
            if paths.live.exists() and paths.live.stat().st_size > 0:
                return None
        except OSError:
            return None
        for n in range(1, BACKUP_DEPTH + 1):
            src = paths.bak(n)
            try:
                if not src.exists() or src.stat().st_size == 0:
                    continue
                plan = _load(src)
            except (OSError, PlanCorrupt):
                continue
            if plan is None:
                continue
            _commit(paths, plan, rotate=False)
            break
        else:
            return None
    _written(profile_id, f"restore from {src.name}")
    return src.name


def copy_legacy_plans(profile_id: str, legacy_dir) -> int:
    """Profile migration of a pre-profiles home: copy the legacy plans dir's
    files into ``profile_id``'s (the legacy dir is left as it was; adoption
    retires it). Returns how many files were copied."""
    paths = _paths(profile_id)
    src_dir = Path(legacy_dir)
    if not src_dir.is_dir():
        return 0
    n = 0
    with _held(paths), _granted(src_dir):
        for item in sorted(src_dir.iterdir()):
            if item.is_file() and item.name != paths.lock.name:
                shutil.copy2(item, paths.dir / item.name)
                n += 1
    if n:
        _written(profile_id, "profile migration")
    return n


def drop(profile_id: str) -> None:
    """Profile deletion: remove the profile's plans dir (plan and backups)."""
    paths = _paths(profile_id)
    if not paths.dir.exists():
        return
    with _LOCK, _granted(paths.dir):
        shutil.rmtree(paths.dir)
    log.info("EVENT=plan_dropped profile=%s", profile_id)


def _stamp(plan: dict):
    """A plan's generation stamp (``generated``, legacy ``generated_at``) as
    a naive datetime; date-only values are midnight; None when neither
    parses."""
    from datetime import date, datetime
    for k in ("generated", "generated_at"):
        v = plan.get(k)
        if not v:
            continue
        s = str(v)
        try:
            return datetime.fromisoformat(s).replace(tzinfo=None)
        except ValueError:
            pass
        try:
            d = date.fromisoformat(s[:10])
            return datetime(d.year, d.month, d.day)
        except ValueError:
            pass
    return None


def adopt_legacy_root(profile_id: str, *, profiles: int, root_dir=None) -> str:
    """One-time adoption of a pre-profiles home's root plan into its owner.

    The owner is named: ``profile_id``. In a home of more than one profile
    the root plan's owner cannot be known, so this refuses and logs; it used
    to adopt into whichever profile was active at boot (contract 10.3, the
    audit's boot2). The rest are the v3.1.0 rules, moved here from
    app._adopt_legacy_root_plan: stamp-primary with an mtime fallback when
    a stamp is missing (never mtime-primary: the boot rewrite restamps the
    profile file); equal stamps adopt; byte-identical root == profile only
    retires; a corrupt root is left for support; the profile plan is
    rotated into its backups, the root's standard backups copied where the
    profile has none of that name, and the root retired to .pre-v3 only
    after the copy succeeded, or a profile-side latch written when that
    retire fails (H2). ``root_dir`` is the legacy root (tests pass a tmp
    dir). Never raises; returns a status string.
    """
    import hashlib
    root_dir = Path(root_dir) if root_dir is not None else _legacy_root()
    root = root_dir / PLAN
    latch = root_dir / (PLAN + ".pre-v3")
    paths = _paths(profile_id)
    prof = paths.live
    prof_latch = paths.dir / (PLAN + ".adopted-from-root")
    try:
        with _granted(root_dir, paths.dir):
            if latch.exists():
                log.debug("plan adoption: skipped -- .pre-v3 latch present")
                return "latched"
            if not root.exists():
                log.debug("plan adoption: skipped -- no legacy root plan")
                return "no-root"
            try:
                if root_dir.resolve() == paths.dir.resolve():
                    return "same-dir"
            except OSError:
                pass
            if profiles > 1:
                log.warning("plan adoption: refused -- the legacy root plan %s has no known "
                            "owner in a home of %d profiles; left in place for support",
                            root, profiles)
                return "refused-multi-profile"
            try:
                root_bytes = root.read_bytes()
                root_plan = json.loads(root_bytes.decode("utf-8"))
                if not isinstance(root_plan, dict) or not root_plan:
                    raise ValueError("not a non-empty JSON object")
            except Exception as e:  # noqa: BLE001 -- a corrupt legacy file must not brick boot
                log.warning(f"plan adoption: skipped -- corrupt legacy root plan left in "
                            f"place for support ({e})")
                return "corrupt-root"
            root_mtime = root.stat().st_mtime
            r_stamp = _stamp(root_plan)
            # Stampless roots latch on a content hash, not a shared sentinel.
            stamp_str = (str(r_stamp) if r_stamp is not None
                         else "sha256:" + hashlib.sha256(root_bytes).hexdigest()[:16])
            if prof_latch.exists() and prof.exists():
                try:
                    if prof_latch.read_text(encoding="utf-8").strip() == stamp_str:
                        log.debug("plan adoption: skipped -- profile-side latch matches")
                        return "latched-prof"
                except OSError:
                    pass
            prof_bytes = prof_plan = None
            if prof.exists():
                try:
                    prof_bytes = prof.read_bytes()
                    parsed = json.loads(prof_bytes.decode("utf-8")) if prof_bytes else None
                    prof_plan = parsed if (isinstance(parsed, dict) and parsed) else None
                except Exception:  # noqa: BLE001 -- unparseable profile plan = missing
                    prof_plan = None
            prof_mtime = prof.stat().st_mtime if prof.exists() else None
        p_stamp = _stamp(prof_plan) if prof_plan is not None else None
        if prof_bytes is not None and prof_bytes == root_bytes:
            verdict, why = "retire-only", "root == profile (byte-identical)"
        elif prof_plan is None:
            verdict, why = "adopt", "profile plan missing/unparseable"
        elif r_stamp is not None and p_stamp is not None:
            if r_stamp > p_stamp:
                verdict, why = "adopt", "root stamp newer"
            elif r_stamp < p_stamp:
                verdict, why = "skip", "profile stamp newer"
            else:
                verdict, why = "adopt", "equal stamps -- single-profile home adopts"
        elif prof_mtime is None or root_mtime > prof_mtime:
            verdict, why = "adopt", "mtime fallback: root newer"
        elif root_mtime < prof_mtime:
            verdict, why = "skip", "mtime fallback: profile newer"
        else:
            verdict, why = "skip", "mtime fallback: tie -- profile wins"
        # Log before acting: adoption rewrites plan data at boot.
        log.info(f"plan adoption decision: verdict={verdict} ({why}); "
                 f"root_stamp={r_stamp} prof_stamp={p_stamp} root_mtime={root_mtime} "
                 f"prof_mtime={prof_mtime} profile={profile_id} root={root} prof={prof}")
        if verdict == "skip":
            return "skipped"      # the root stays in place (support lever)
        with _held(paths), _granted(root_dir):
            if verdict == "adopt":
                snap = None
                if prof.exists():
                    snap = paths.snap
                    shutil.copy2(prof, snap)
                shutil.copy2(root, paths.tmp)
                paths.tmp.replace(prof)
                if snap is not None:
                    _rotate(paths, snap)
                for n in range(1, BACKUP_DEPTH + 1):
                    src, dst = root_dir / paths.bak(n).name, paths.bak(n)
                    if src.exists() and not dst.exists():
                        try:
                            shutil.copy2(src, dst)
                        except OSError as e:
                            log.debug(f"plan adoption: bak copy {src.name} failed: {e}")
            try:
                root.replace(latch)    # a rename, never a delete
                status = "adopted" if verdict == "adopt" else "retired-identical"
            except OSError as e:
                log.warning(f"plan adoption: retire failed ({e}) -- writing the "
                            "profile-side latch so later boots don't re-adopt (H2)")
                try:
                    prof_latch.write_text(stamp_str, encoding="utf-8")
                except OSError:
                    pass
                status = "adopted" if verdict == "adopt" else "retire-failed"
        if verdict == "adopt":
            _written(profile_id, f"legacy root adoption ({why})")
        return status
    except Exception as e:  # noqa: BLE001 -- boot must continue
        log.warning(f"plan adoption failed: {e}")
        return "error"


# ── G2: the audit hook ───────────────────────────────────────────────────────
_TEST_ROOTS: tuple = ()
_WRITE_EVENTS = {"os.rename", "os.remove", "os.rmdir", "os.truncate", "os.mkdir",
                 "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.move",
                 "shutil.rmtree", "shutil.copytree", "os.link", "os.symlink", "os.chmod",
                 "os.utime", "os.chown"}
_SPAWN_EVENTS = {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.spawn"}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_STDLIB = (os.path.dirname(os.__file__), "<frozen")


def allow_test_writes(root) -> None:
    """The test session seeds plans with plain writes from its own files:
    code under ``root`` is exempt. Production never calls this."""
    global _TEST_ROOTS
    _TEST_ROOTS = tuple(sorted(set(_TEST_ROOTS) | {_key(root)}))


def _plan_dir_of(p, *, tree: bool = False) -> "str | None":
    """The plan dir ``p`` lands in, or None. ``tree``: p is removed with
    everything under it (rmtree), so a plans dir inside it counts."""
    try:
        a = _key(p)
    except TypeError:
        return None
    base = os.path.basename(a)
    d = os.path.dirname(a)
    if base.startswith("current_plan"):
        return d
    if "plans" not in a and not _dirs and not tree:
        return None
    home = str(domestique_home())
    roots = {_key(v) for v in _dirs.values()}
    for c in (a, d):
        if c in roots:
            return c
        if os.path.basename(c) == "plans":
            up = os.path.dirname(c)
            if up == home or os.path.basename(os.path.dirname(up)) == "profiles":
                return c
    if tree:
        if os.path.isdir(os.path.join(a, "plans")):
            return os.path.join(a, "plans")
        for v in roots:
            if v.startswith(a + os.sep):
                return v
    return None


def _from_tests() -> bool:
    if not _TEST_ROOTS:
        return False
    f = sys._getframe(2)
    while f is not None:
        fn = f.f_code.co_filename
        if fn != __file__ and not fn.startswith(_STDLIB):
            return fn.startswith(_TEST_ROOTS)
        f = f.f_back
    return False


def _audit(event, args):
    if event == "open":
        path, mode, flags = args
        if isinstance(path, int) or path is None:
            return
        d = _plan_dir_of(path)
        if d is None:
            return
        writes = (any(c in mode for c in "wax+") if isinstance(mode, str)
                  else bool((flags or 0) & _WRITE_FLAGS))
        _check(event, d, path, writes)
    elif event in _WRITE_EVENTS:
        tree = event in ("shutil.rmtree", "shutil.move")
        for a in args[:2]:
            if isinstance(a, (str, bytes, os.PathLike)):
                d = _plan_dir_of(a, tree=tree)
                if d is not None:
                    _check(event, d, a, True)
    elif event in _SPAWN_EVENTS:
        for a in args:
            items = a if isinstance(a, (list, tuple)) else [a]
            for x in items:
                if isinstance(x, (str, bytes, os.PathLike)):
                    s = os.fsdecode(x) if not isinstance(x, os.PathLike) else os.fspath(x)
                    if "current_plan" in s or _plan_dir_of(s) is not None:
                        if not _from_tests():
                            _refuse(f"G2: {event} names a plan path ({s})")


def _check(event, d, path, writes) -> None:
    if d in _TOKEN.get():
        if writes:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return
            _refuse(f"G2: plan store write on the event loop's thread ({event} {path})")
        return
    if _from_tests():
        return
    _refuse(f"G2: {event} {os.fspath(path)} outside plan_store (by {_caller()})")


_HOOKED = False


def install_guard() -> None:
    """Install the audit hook (once per process: audit hooks cannot be
    removed)."""
    global _HOOKED
    if not _HOOKED:
        sys.addaudithook(_audit)
        _HOOKED = True
