#!/usr/bin/env python3
"""Module-level constants: how many, and which are defined in two places?

    python3 tools/arch/constdup.py

Run before anyone proposes moving constants into a shared store. The answer
on 2026-09-16 was that 600 constants live across 46 files and only 17 names
are defined twice -- 13 of them one dead module's copy of a colour palette --
so a global store would have moved 583 well-placed constants away from the
rules they belong to in order to fix four cases. What it does catch is worth
catching: two modules that both define a name, especially with DIFFERENT
values, which is a divergence waiting to be noticed by a rider.
"""
import ast, pathlib, collections
consts = collections.defaultdict(list)     # NAME -> [(module, value)]
per_module = collections.Counter()
for p in sorted(pathlib.Path("src").rglob("*.py")):
    tree = ast.parse(p.read_text(errors="replace"), str(p))
    for n in tree.body:
        if not isinstance(n, ast.Assign):
            continue
        for t in n.targets:
            if isinstance(t, ast.Name) and t.id.isupper():
                try:
                    val = ast.literal_eval(n.value)
                except Exception:
                    val = "<expr>"
                consts[t.id].append((p.name, val))
                per_module[p.name] += 1
print(f"{sum(per_module.values())} module-level CONSTANTS across {len(per_module)} files")
print("top holders:", ", ".join(f"{m} {c}" for m, c in per_module.most_common(6)))
same_name = {k: v for k, v in consts.items() if len({m for m, _ in v}) > 1}
print(f"\ndefined in more than one module: {len(same_name)}")
for k, v in sorted(same_name.items()):
    vals = {repr(val) for _m, val in v}
    flag = "  <-- VALUES DIFFER" if len(vals) > 1 else ""
    print(f"  {k}: {[m for m, _ in v]}{flag}")
    if flag:
        for m, val in v:
            print(f"      {m} = {val!r}")
