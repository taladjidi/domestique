"""G4 (contract 10.6): every place in src/ that touches the plan outside its
owner, plan_store.py -- found by reading the source, not by running it.

Ported from the single-source audit's static_scan.py (2026-09-25), which
caught the 12 evaluator plants and the extra ones; widened from "writes" to
"access", because 10.2 makes plan_store.read the one reader too.

Findings (outside src/plan_store.py):
  ACCESS  a plan path is built: a "current_plan..." / "plans" name, PLAN_DIR,
          plan_dir, _plan_dir() used as a path, a *.md glob of the plan dir.
  OWNER   a plan-write primitive is named (training_planner's retired
          atomic_write_plan / _rotate_plan_backups / plan_write_lock /
          _plan_write_lock, or any private name of plan_store), or a raw file
          write (open w/a/x/+, write_text/bytes, replace/rename, os.*,
          shutil.*) lands on a plan path.
  DOOR    a plan read-modify-write other than plan_store.update
          (training_planner.update_plan, the door before this round).
  LOOP    an ``async def`` reaches a plan writer through synchronous calls --
          not through run_in_threadpool / to_thread / Thread / add_task.
  CENSUS  a plan_store.update call (the one door), for the report only.

Aliases followed: import X as Y, from X import f as g (any scope), module-level
name = func / name = mod.func, nested defs, self.m / Cls().m / obj.m method
calls (resolved to same-module methods of that name), callbacks passed by name.

Findings are keyed without line numbers ("KIND module:func  what") so the
allowlist in test_one_plan_static.py survives unrelated edits.
"""
from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

TP = "training_planner"
STORE = "plan_store"
# The store before this round lived in training_planner; its internals are
# the store's (not findings) until they move.
LEGACY_PRIMS = {"atomic_write_plan", "_rotate_plan_backups", "plan_write_lock",
                "_plan_write_lock", "update_plan"}
LEGACY_INTERNAL = {f"{TP}:update_plan", f"{TP}:atomic_write_plan",
                   f"{TP}:_rotate_plan_backups", f"{TP}:plan_write_lock",
                   f"{TP}:_PlanLock.acquire", f"{TP}:_PlanLock.release",
                   f"{TP}:_PlanLock.__enter__", f"{TP}:_PlanLock.__exit__",
                   f"{TP}:_PlanLock.__init__", f"{TP}:_refuse_on_event_loop"}
STORE_WRITERS = {"update", "restore_from_backup", "adopt_legacy_root", "copy_legacy_plans",
                 "drop", "_commit", "_held", "_rotate"}
OFFLOAD = {"run_in_threadpool", "to_thread", "run_in_executor", "Thread",
           "add_task", "submit", "Timer", "start_new_thread"}
PATH_WRITE_METHODS = {"write_text", "write_bytes", "touch", "unlink", "rmdir",
                      "symlink_to", "hardlink_to", "mkdir"}
PATH_MOVE_METHODS = {"replace", "rename"}
OS_WRITES = {"replace", "rename", "remove", "unlink", "rmdir", "renames", "truncate"}
SHUTIL_WRITES = {"copy", "copy2", "copyfile", "move", "copytree", "rmtree", "copyfileobj"}
PLAN_PARAM_HINT = ("json_path", "plan_path", "plan_file", "prof_dir", "root_dir")
PATH_FUNCS = {"Path", "PurePath", "str", "fspath", "with_suffix", "with_name", "with_stem",
              "joinpath", "resolve", "absolute", "expanduser", "join", "realpath", "abspath"}


class Mod:
    def __init__(self, name, path, src):
        self.name, self.path = name, path
        self.tree = ast.parse(src, filename=str(path))
        self.mod_alias = {}       # local name -> module name
        self.name_alias = {}      # local name -> "mod:func"
        self.funcs = {}           # qual -> node
        self.parent = {}          # qual -> enclosing qual (or None)
        self.classes = {}         # class name -> [method names]
        self.async_ = set()


def load(src_dir: Path, extra: dict | None = None):
    mods = {}
    for p in sorted(Path(src_dir).rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        name = p.relative_to(src_dir).with_suffix("").as_posix().replace("/", ".")
        text = p.read_text(encoding="utf-8", errors="replace")
        if extra and name in extra:
            text = text + "\n\n" + extra[name]
        try:
            mods[name] = Mod(name, p, text)
        except SyntaxError as e:
            print(f"skip {p}: {e}", file=sys.stderr)
    return mods


def index(mods):
    for m in mods.values():
        for node in ast.walk(m.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    m.mod_alias[a.asname or a.name.split(".")[0]] = a.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                last = node.module.split(".")[-1]
                for a in node.names:
                    if last in mods and a.name not in mods:
                        m.name_alias[a.asname or a.name] = f"{last}:{a.name}"
                    elif a.name in mods:
                        m.mod_alias[a.asname or a.name] = a.name

        def walk(node, prefix, parent, cls=None):
            for n in ast.iter_child_nodes(node):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    q = f"{m.name}:{prefix}{n.name}"
                    m.funcs[q] = n
                    m.parent[q] = parent
                    if isinstance(n, ast.AsyncFunctionDef):
                        m.async_.add(q)
                    if cls:
                        m.classes.setdefault(cls, []).append(n.name)
                    walk(n, f"{prefix}{n.name}.", q)
                elif isinstance(n, ast.ClassDef):
                    walk(n, f"{prefix}{n.name}.", parent, cls=n.name)
                else:
                    walk(n, prefix, parent, cls)
        walk(m.tree, "", None)
        for n in m.tree.body:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                r = resolve_ref(mods, m, n.value, None)
                if r:
                    m.name_alias[n.targets[0].id] = r


def resolve_ref(mods, m, expr, scope):
    """expr names a function -> 'mod:qual' or None."""
    if isinstance(expr, ast.Name):
        nm = expr.id
        s = scope
        while s is not None:
            q = f"{s}.{nm}"
            if q in m.funcs:
                return q
            s = m.parent.get(s)
        if f"{m.name}:{nm}" in m.funcs:
            return f"{m.name}:{nm}"
        if nm in m.name_alias:
            return m.name_alias[nm]
        return None
    if isinstance(expr, ast.Attribute):
        base = expr.value
        if isinstance(base, ast.Name) and base.id in m.mod_alias:
            mod = m.mod_alias[base.id].split(".")[-1]
            if mod in mods:
                return f"{mod}:{expr.attr}"
            return None
        for cls, meths in m.classes.items():
            if expr.attr in meths:
                return f"{m.name}:{cls}.{expr.attr}"
    return None


def call_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


_OWN: dict = {}


def own_nodes(fn):
    """Nodes of fn's body, not descending into nested defs/classes (lambdas are in)."""
    got = _OWN.get(id(fn))
    if got is None or got[0] is not fn:
        got = _OWN[id(fn)] = (fn, list(_own_nodes(fn)))
    return got[1]


def _own_nodes(fn):
    if isinstance(fn, ast.Module):
        stack = [n for n in fn.body
                 if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    else:
        stack = list(fn.body) + list(fn.args.defaults) + [d for d in fn.args.kw_defaults if d]
    while stack:
        n = stack.pop()
        yield n
        for ch in ast.iter_child_nodes(n):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.append(ch)


def _plan_name_const(v) -> bool:
    return isinstance(v, str) and (v.startswith("current_plan") or v == "plans"
                                   or (v.startswith("plan_") and v.endswith(".md")))


def is_plan_dir_expr(e, tainted):
    """Path-like expression that resolves into the plan dir (not a plan's data)."""
    if isinstance(e, ast.Name):
        return e.id in tainted or e.id in ("PLAN_DIR", "_DEFAULT_PLAN_DIR")
    if isinstance(e, ast.Attribute):
        if e.attr in ("PLAN_DIR", "plan_dir", "_DEFAULT_PLAN_DIR"):
            return True
        if e.attr == "parent":
            return is_plan_dir_expr(e.value, tainted)
        return False
    if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Div):
        return is_plan_dir_expr(e.left, tainted) or is_plan_dir_expr(e.right, tainted)
    if isinstance(e, ast.Call):
        nm = call_name(e.func)
        if nm in ("_plan_dir", "plan_dir"):
            return True
        if nm in PATH_FUNCS:
            parts = list(e.args)
            if isinstance(e.func, ast.Attribute) and nm not in ("join",):
                parts.append(e.func.value)
            return any(is_plan_dir_expr(x, tainted) for x in parts)
        return False
    if isinstance(e, ast.JoinedStr):
        return any((isinstance(v, ast.Constant) and _plan_name_const(str(v.value)))
                   or (isinstance(v, ast.FormattedValue) and is_plan_dir_expr(v.value, tainted))
                   for v in e.values)
    if isinstance(e, ast.Constant):
        return _plan_name_const(e.value)
    if isinstance(e, (ast.List, ast.Tuple, ast.Set)):
        return any(is_plan_dir_expr(x, tainted) for x in e.elts)
    if isinstance(e, ast.IfExp):
        return is_plan_dir_expr(e.body, tainted) or is_plan_dir_expr(e.orelse, tainted)
    if isinstance(e, ast.BoolOp):
        return any(is_plan_dir_expr(x, tainted) for x in e.values)
    return False


def local_taint(fn, seeds):
    tainted = set(seeds)
    for _ in range(6):
        before = len(tainted)
        for n in own_nodes(fn):
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                val = n.value
                tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
                if val is not None and is_plan_dir_expr(val, tainted):
                    for t in tgts:
                        for x in ast.walk(t):
                            if isinstance(x, ast.Name):
                                tainted.add(x.id)
            elif isinstance(n, (ast.For, ast.comprehension)):
                if is_plan_dir_expr(n.iter, tainted):
                    for x in ast.walk(n.target):
                        if isinstance(x, ast.Name):
                            tainted.add(x.id)
            elif isinstance(n, ast.withitem) and n.optional_vars is not None:
                if is_plan_dir_expr(n.context_expr, tainted):
                    for x in ast.walk(n.optional_vars):
                        if isinstance(x, ast.Name):
                            tainted.add(x.id)
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr in ("append", "extend", "add") \
                    and isinstance(n.func.value, ast.Name) \
                    and any(is_plan_dir_expr(a, tainted) for a in n.args):
                tainted.add(n.func.value.id)
            elif isinstance(n, ast.NamedExpr) and is_plan_dir_expr(n.value, tainted):
                tainted.add(n.target.id)
        if len(tainted) == before:
            break
    return tainted


def params(fn):
    if isinstance(fn, ast.Module):
        return []
    a = fn.args
    return [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]


def raw_write(n, m, tainted):
    """n is a Call: return a description if it writes a plan-dir target."""
    f = n.func
    t = lambda e: is_plan_dir_expr(e, tainted)  # noqa: E731
    nm = call_name(f)
    if nm == "open" and (isinstance(f, ast.Name) or (isinstance(f, ast.Attribute) and
                         isinstance(f.value, ast.Name) and f.value.id in ("io", "builtins", "os"))):
        mode = None
        if len(n.args) >= 2 and isinstance(n.args[1], ast.Constant):
            mode = n.args[1].value
        for k in n.keywords:
            if k.arg == "mode" and isinstance(k.value, ast.Constant):
                mode = k.value.value
        if n.args and isinstance(mode, str) and set(mode) & set("wax+") and t(n.args[0]):
            return f"open(..., {mode!r})"
        if n.args and isinstance(f, ast.Attribute) and f.value.id == "os" and t(n.args[0]):
            return "os.open"
        return None
    if isinstance(f, ast.Attribute):
        base = f.value
        bmod = m.mod_alias.get(base.id) if isinstance(base, ast.Name) else None
        if bmod == "os" and f.attr in OS_WRITES and any(t(a) for a in n.args):
            return f"os.{f.attr}"
        if bmod == "shutil" and f.attr in SHUTIL_WRITES and any(t(a) for a in n.args):
            return f"shutil.{f.attr}"
        if bmod == "subprocess" and any(t(a) for a in list(n.args) + [k.value for k in n.keywords]):
            return f"subprocess.{f.attr}"
        if bmod in ("json",) and f.attr == "dump" and len(n.args) >= 2 and t(n.args[1]):
            return "json.dump"
        if bmod is None and f.attr in PATH_WRITE_METHODS and t(base):
            return f"Path.{f.attr}"
        if bmod is None and f.attr == "open" and t(base):
            mode = n.args[0].value if n.args and isinstance(n.args[0], ast.Constant) else None
            for k in n.keywords:
                if k.arg == "mode" and isinstance(k.value, ast.Constant):
                    mode = k.value.value
            if isinstance(mode, str) and set(mode) & set("wax+"):
                return f"Path.open({mode!r})"
        if bmod is None and f.attr in PATH_MOVE_METHODS and len(n.args) == 1 and not n.keywords \
                and (t(base) or t(n.args[0])):
            return f"Path.{f.attr}"
    return None


def _taint_of(m, q, fn, ptaint, memo):
    if q in memo:
        return memo[q]
    seeds = set(ptaint.get(q, ()))
    par = m.parent.get(q)
    if par is not None and par in m.funcs:
        seeds |= _taint_of(m, par, m.funcs[par], ptaint, memo)
    memo[q] = local_taint(fn, seeds)
    return memo[q]


def _prim(tgt: str) -> str | None:
    """A reference to a store primitive -> (kind, name), else None."""
    mod, _, name = tgt.partition(":")
    if mod == TP and name in LEGACY_PRIMS:
        return ("DOOR" if name == "update_plan" else "OWNER"), name
    if mod == STORE:
        if name == "update":
            return "CENSUS", "plan_store.update"
        if name.startswith("_"):
            return "OWNER", f"plan_store.{name}"
    return None


def _access_nodes(fn, tainted):
    """Plan-path constructions in fn's own body (reads and writes alike)."""
    out = []
    docs = {id(n.value) for n in own_nodes(fn)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    for n in own_nodes(fn):
        what = None
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs \
                and n.value.startswith("current_plan") and " " not in n.value:
            what = repr(n.value)[:40]
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div) and (
                (isinstance(n.right, ast.Constant) and _plan_name_const(n.right.value))
                or (isinstance(n.right, ast.JoinedStr) and is_plan_dir_expr(n.right, set()))):
            what = f"path / {ast.unparse(n.right)[:40]}"
        elif isinstance(n, ast.Call):
            nm = call_name(n.func)
            if nm in ("_plan_dir", "plan_dir"):
                what = f"{nm}()"
            elif nm in ("with_suffix", "with_name", "joinpath", "glob", "rglob", "Path") and any(
                    isinstance(a, ast.Constant) and isinstance(a.value, str)
                    and (_plan_name_const(a.value) or ".bak" in a.value or a.value.startswith("*.md")
                         or "premigration" in a.value) and (
                        nm not in ("with_suffix", "glob", "rglob")
                        or (isinstance(n.func, ast.Attribute) and is_plan_dir_expr(n.func.value, tainted)))
                    for a in n.args):
                what = f"{nm}({ast.unparse(n.args[0])[:40]})"
        elif isinstance(n, (ast.Attribute, ast.Name)):
            ident = n.attr if isinstance(n, ast.Attribute) else n.id
            if ident in ("PLAN_DIR", "_DEFAULT_PLAN_DIR"):
                what = ident
        if what:
            out.append((what, getattr(n, "lineno", 0)))
    return out


def analyse(mods):
    index(mods)
    units = {}          # q -> (mod, node)   functions plus each module body
    for m in mods.values():
        for q, fn in m.funcs.items():
            units[q] = (m, fn)
        units[f"{m.name}:<module>"] = (m, m.tree)
    ptaint = {q: {p for p in params(fn) if p in PLAN_PARAM_HINT} for q, (m, fn) in units.items()}
    for _round in range(4):
        changed = False
        memo: dict = {}
        for q, (m, fn) in units.items():
            tainted = _taint_of(m, q, fn, ptaint, memo)
            for n in own_nodes(fn):
                if not isinstance(n, ast.Call):
                    continue
                tgt = resolve_ref(mods, m, n.func, q if q in m.funcs else None)
                if tgt and tgt.split(":")[0] in mods:
                    tm = mods[tgt.split(":")[0]]
                    if tgt in tm.funcs:
                        ps = params(tm.funcs[tgt])
                        if ps and ps[0] in ("self", "cls"):
                            ps = ps[1:]
                        for i, a in enumerate(n.args):
                            if i < len(ps) and is_plan_dir_expr(a, tainted) and ps[i] not in ptaint[tgt]:
                                ptaint[tgt].add(ps[i]); changed = True
                        for k in n.keywords:
                            if k.arg in ps and is_plan_dir_expr(k.value, tainted) and k.arg not in ptaint[tgt]:
                                ptaint[tgt].add(k.arg); changed = True
        if not changed:
            break
    edges, sinks = {}, {}
    memo = {}
    for q, (m, fn) in units.items():
        scope = q if q in m.funcs else None
        tainted = _taint_of(m, q, fn, ptaint, memo)
        e, s = [], []
        for n in own_nodes(fn):
            if isinstance(n, ast.Attribute) and n.attr == "_plan_write_lock":
                s.append(("OWNER", "_plan_write_lock (raw lock)", n.lineno))
            if isinstance(n, ast.Name) and n.id == "_plan_write_lock" and m.name == TP:
                s.append(("OWNER", "_plan_write_lock (raw lock)", n.lineno))
            if isinstance(n, ast.Attribute) and n.attr.startswith("_") and isinstance(n.value, ast.Name) \
                    and m.mod_alias.get(n.value.id, "").split(".")[-1] == STORE:
                s.append(("OWNER", f"plan_store.{n.attr}", n.lineno))
            if not isinstance(n, ast.Call):
                continue
            nm = call_name(n.func)
            tgt = resolve_ref(mods, m, n.func, scope)
            offload = nm in OFFLOAD
            if tgt:
                e.append((tgt, "sync", n.lineno))
                pk = _prim(tgt)
                if pk:
                    s.append((pk[0], pk[1], n.lineno))
            for a in list(n.args) + [k.value for k in n.keywords]:
                r = resolve_ref(mods, m, a, scope) if isinstance(a, (ast.Name, ast.Attribute)) else None
                if r and r != tgt:
                    e.append((r, "offload" if offload else "callback", n.lineno))
                    pk = _prim(r)
                    if pk:
                        s.append((pk[0], pk[1] + (" (offloaded)" if offload else " (as value)"), n.lineno))
            w = raw_write(n, m, tainted)
            if w:
                s.append(("OWNER", "raw " + w, n.lineno))
        for what, ln in _access_nodes(fn, tainted):
            s.append(("ACCESS", what, ln))
        edges[q], sinks[q] = e, sorted(set(s), key=lambda x: (x[2], x[0], x[1]))
    # The store's own functions are writers only where they write: its
    # readers (read, version, capture) are safe on the event loop.
    writer = {q for q, s in sinks.items()
              if not q.startswith(f"{STORE}:") and any(k in ("OWNER", "DOOR", "CENSUS") for k, _, _ in s)}
    writer |= {f"{STORE}:{n}" for n in STORE_WRITERS}
    changed = True
    while changed:
        changed = False
        for q, es in edges.items():
            if q in writer:
                continue
            if any(c in writer and k == "sync" for c, k, _ in es):
                writer.add(q); changed = True
    findings = []
    for q, (m, fn) in units.items():
        if m.name == STORE:
            continue
        for kind, desc, ln in sinks[q]:
            if kind in ("OWNER", "ACCESS") and q in LEGACY_INTERNAL:
                continue
            findings.append({"kind": kind, "func": q, "file": m.path.name, "line": ln, "what": desc})
    for m in mods.values():
        for q in m.async_:
            seen, stack, hit = {q: None}, [q], None
            while stack and hit is None:
                cur = stack.pop()
                for c, k, ln in edges.get(cur, []):
                    if k != "sync" or c in seen:
                        continue
                    seen[c] = (cur, ln)
                    if c in writer:
                        hit = c
                        break
                    stack.append(c)
            if hit:
                path, c = [], hit
                while c is not None and seen.get(c) is not None:
                    par, ln = seen[c]
                    path.append(f"{par.split(':')[1]} -> {c}")
                    c = par
                findings.append({"kind": "LOOP", "func": q, "file": m.path.name,
                                 "line": m.funcs[q].lineno, "what": " | ".join(reversed(path))})
    return findings, writer


def key(f) -> str:
    return f"{f['kind']} {f['func']}  {f['what']}"


def scan(src_dir: Path, extra: dict | None = None) -> Counter:
    """The non-CENSUS findings under src_dir, keyed without line numbers."""
    findings, _ = analyse(load(src_dir, extra))
    return Counter(key(f) for f in findings if f["kind"] != "CENSUS")


def scan_full(src_dir: Path, extra: dict | None = None) -> list[dict]:
    return analyse(load(src_dir, extra))[0]


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "src"
    fs = scan_full(src)
    for f in sorted(fs, key=lambda f: (f["file"], f["line"])):
        if f["kind"] != "CENSUS":
            print(f"{f['kind']:6} {f['file']}:{f['line']} {f['func']}  {f['what']}")
