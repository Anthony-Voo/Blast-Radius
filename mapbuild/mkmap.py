"""Merge traced coverage into the compact map the selector ships with."""
import glob, gzip, json, os, sys
cov, tests_json, times_json, outdir = sys.argv[1:5]
tests = json.load(open(tests_json))["tests"]
ids = [t["id"] for t in tests]
idx = {t: i for i, t in enumerate(ids)}
lines, files, classes, implines, classimports = {}, {}, {}, {}, {}
srcs = [os.path.join(cov, "map.json")] + sorted(glob.glob(os.path.join(cov, "map.json.child.*.json")))
class_ctx = 0
for p in srcs:
    d = json.load(open(p))
    # class-level set-up contexts apply to every test of that class
    for part in ("lines", "files", "classes"):
        for ctx in [k for k in d[part] if k.startswith("<class:")]:
            prefix = ctx[len("<class:"):-1] + "::"
            members = [t for t in ids if t.startswith(prefix)]
            class_ctx += 1
            for t in members:
                cur = d[part].get(t)
                val = d[part][ctx]
                if part == "lines":
                    cur = d[part].setdefault(t, {})
                    for f, ls in val.items():
                        cur[f] = sorted(set(cur.get(f, ())) | set(ls))
                else:
                    d[part][t] = sorted(set(cur or ()) | set(val))
    for t, fm in d["lines"].items():
        i = idx.get(t)
        if i is None: continue
        for f, ls in fm.items():
            fl = lines.setdefault(f, {})
            for l in ls: fl.setdefault(l, set()).add(i)
    for t, fs in d["files"].items():
        i = idx.get(t)
        if i is None: continue
        for f in fs: files.setdefault(f, set()).add(i)
    for f, lm in d.get("imports", {}).items():
        for l, mods in lm.items():
            implines.setdefault(f, {}).setdefault(l, set()).update(mods)
    for c, mods in d.get("class_imports", {}).items():
        classimports.setdefault(c, set()).update(mods)
    for t, cs in d["classes"].items():
        i = idx.get(t)
        if i is None: continue
        for c in cs: classes.setdefault(c, set()).add(i)
def bits(s):
    v = 0
    for i in s: v |= 1 << i
    return format(v, "x")
os.makedirs(os.path.join(outdir, "lines"), exist_ok=True)
for old in glob.glob(os.path.join(outdir, "lines", "*")): os.remove(old)
total = 0
for f, fl in lines.items():
    sets, sidx, l2s = [], {}, {}
    for l, s in fl.items():
        b = bits(s)
        j = sidx.get(b)
        if j is None:
            j = sidx[b] = len(sets); sets.append(b)
        l2s[str(l)] = j
    name = f.replace("/", "__") + ".json.gz"
    with gzip.open(os.path.join(outdir, "lines", name), "wt", compresslevel=9) as g:
        json.dump({"sets": sets, "lines": l2s}, g, separators=(",", ":"))
    total += os.path.getsize(os.path.join(outdir, "lines", name))
tm = json.load(open(times_json))["times"]
cost = [round(tm.get(str(t["number"]), 0.0), 4) for t in tests]
importers = json.load(open(os.path.join(cov, "importers.json")))
imp = {}
for tf, mods in importers.items():
    for m in mods: imp.setdefault(m, []).append(tf)
import ast as _ast, re as _re
TOK = _re.compile(r"subprocess|importlib|sys\.modules|reload\(|sys\.executable|__import__|pkgutil|getsource|sys\.path")
SRC_ROOT = os.environ.get("COVMAP_ROOT", "sqlglot")
isolated = set()
by_file = {}
for t in tests: by_file.setdefault(t["file"], []).append(t["id"])
for tf, tids in by_file.items():
    text = open(os.path.join(SRC_ROOT, tf), encoding="utf-8").read()
    lines_ = text.split("\n")
    tree = _ast.parse(text)
    inside = set()
    hit_methods = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and sub.name.startswith("test"):
                    seg = "\n".join(lines_[sub.lineno - 1: sub.end_lineno])
                    inside.update(range(sub.lineno, sub.end_lineno + 1))
                    if TOK.search(seg): hit_methods.add((node.name, sub.name))
    outside = "\n".join(l for i, l in enumerate(lines_, 1) if i not in inside and not l.lstrip().startswith(("import ", "from ")))
    if TOK.search(outside):
        isolated.update(tids)
    for tid_ in tids:
        _, c, m_ = tid_.split("::")
        if (c, m_) in hit_methods: isolated.add(tid_)
for f, s_ in files.items():
    if f.endswith(".py") or f.startswith("dir:"):
        for i in s_: isolated.add(ids[i])
print("isolated tests", len(isolated))
meta = {"tests": ids, "cost": cost, "isolated": sorted(isolated),
        "files": {f: bits(s) for f, s in files.items()},
        "classes": {c: bits(s) for c, s in classes.items()},
        "importers": imp, "lined": sorted(lines),
        "classimports": {c: sorted(m) for c, m in classimports.items()},
        "implines": {f: {l: sorted(m) for l, m in d.items()} for f, d in implines.items()}}
with gzip.open(os.path.join(outdir, "meta.json.gz"), "wt", compresslevel=9) as g:
    json.dump(meta, g, separators=(",", ":"))
total += os.path.getsize(os.path.join(outdir, "meta.json.gz"))
print("class set-up contexts merged", class_ctx)
print("files with lines", len(lines), "opened", len(files), "classes", len(classes), "bytes", total)
nocov = [t for t in ids if not any(idx[t] in s for fl in lines.values() for s in fl.values())]
print("tests with no line coverage:", nocov)
