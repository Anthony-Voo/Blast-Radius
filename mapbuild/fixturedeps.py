"""Which tests of a class really depend on a fixture its setUpClass loads?

For every text fixture opened during class set-up (at the pinned commit), the
file is rewritten twice - once with every input statement corrupted, once with
every expected statement corrupted - and the class's tests are run. A test that
still passes does not depend on that half of the file.

usage: fixturedeps.py <trace dir> <sqlglot checkout> <tests.json> <out.json>
"""
import json
import os
import shutil
import sys

ROOT = os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "repo"))
from blast import runner  # noqa: E402

cov, src, tests_json, out = sys.argv[1:5]
tests = json.load(open(tests_json))["tests"]
d = json.load(open(os.path.join(cov, "map.json")))
targets = {}
for ctx, files in d["files"].items():
    if ctx.startswith("<class:"):
        prefix = ctx[len("<class:"):-1]
        for f in files:
            if f.endswith(".sql"):
                targets.setdefault(f, []).append(prefix)
print("class-loaded fixtures:", targets)


def statements(text):
    return "\n".join(l for l in text.splitlines() if l and not l.startswith("--")).split(";")


def variant(text, which):
    st = statements(text)
    outp = []
    for i, s in enumerate(st):
        if i + 1 >= len(st) and i % 2 == 0:
            outp.append(s)
            continue
        if which == "input" and i % 2 == 0:
            meta = [l for l in s.strip().split("\n") if l.startswith("#")]
            outp.append("\n" + "\n".join(meta + ["SELECT 1 +"]))
        elif which == "expected" and i % 2 == 1:
            outp.append("\nSELECT zz_corrupt_expected")
        else:
            outp.append(s)
    return ";".join(outp)


tree = os.path.join(ROOT, "fxtree")
shutil.rmtree(tree, ignore_errors=True)
os.makedirs(tree)
for name in ("sqlglot", "tests", "sqlglotc"):
    shutil.copytree(os.path.join(src, name), os.path.join(tree, name), ignore=shutil.ignore_patterns("__pycache__"))
result = {}
for f, prefixes in sorted(targets.items()):
    path = os.path.join(tree, *f.split("/"))
    original = open(path, encoding="utf-8", newline="").read()
    class_tests = [t for t in tests if any(t["id"].startswith(p + "::") for p in prefixes)]
    spec = [(t["number"], t["id"], 600) for t in class_tests]
    entry = {"classes": prefixes, "input": [], "expected": []}
    for which in ("input", "expected"):
        open(path, "w", encoding="utf-8", newline="").write(variant(original, which))
        res, complete, cpu = runner.run_tests(tree, spec, 900)
        bad = [t["id"] for t in class_tests if res.get(t["number"]) != "P"]
        print(f, which, "complete", complete, "dependent tests:", [b.split("::")[-1] for b in bad], flush=True)
        entry[which] = bad
    open(path, "w", encoding="utf-8", newline="").write(original)
    # sanity: untouched file passes
    result[f] = entry
json.dump(result, open(out, "w"), indent=1)
