"""Merge traced coverage into the compact map the selector ships with."""
import glob, gzip, json, os, sys
cov, tests_json, times_json, outdir = sys.argv[1:5]
tests = json.load(open(tests_json))["tests"]
ids = [t["id"] for t in tests]
idx = {t: i for i, t in enumerate(ids)}
lines, files, classes, implines = {}, {}, {}, {}
srcs = [os.path.join(cov, "map.json")] + sorted(glob.glob(os.path.join(cov, "map.json.child.*.json")))
for p in srcs:
    d = json.load(open(p))
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
meta = {"tests": ids, "cost": cost,
        "files": {f: bits(s) for f, s in files.items()},
        "classes": {c: bits(s) for c, s in classes.items()},
        "importers": imp, "lined": sorted(lines),
        "implines": {f: {l: sorted(m) for l, m in d.items()} for f, d in implines.items()}}
with gzip.open(os.path.join(outdir, "meta.json.gz"), "wt", compresslevel=9) as g:
    json.dump(meta, g, separators=(",", ":"))
total += os.path.getsize(os.path.join(outdir, "meta.json.gz"))
print("files with lines", len(lines), "opened", len(files), "classes", len(classes), "bytes", total)
nocov = [t for t in ids if not any(idx[t] in s for fl in lines.values() for s in fl.values())]
print("tests with no line coverage:", nocov)
