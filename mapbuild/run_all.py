"""Run every test through blast.runner on a tree; print failures and timing."""
import json, os, subprocess, sys, time
tree, tests_json, out = sys.argv[1:4]
only = sys.argv[4] if len(sys.argv) > 4 else None
tests = json.load(open(tests_json))["tests"]
spec = {"tests": [[t["number"], t["id"], 600] for t in tests if not only or only in t["id"]]}
json.dump(spec, open(out + ".spec.json", "w"))
if os.path.exists(out): os.remove(out)
env = dict(os.environ, BLAST_TIMING="1")
t = time.time()
subprocess.run([sys.executable, "repo/blast/runner.py", tree, out + ".spec.json", out], env=env)
print("wall", round(time.time() - t, 1))
res = {}
for line in open(out):
    a = line.split()
    if a[0] != "-1" and (a[1] == "F" or int(a[0]) not in res): res[int(a[0])] = a[1]
bad = [t["id"] for t in tests if res.get(t["number"]) == "F"]
unk = [t["id"] for t in tests if t["number"] not in res and (not only or only in t["id"])]
print("failed", len(bad), bad[:20]); print("unknown", len(unk), unk[:10])
tm = json.load(open(out + ".times.json")); print("cpu total", round(tm["total"], 1), "sum tests", round(sum(tm["times"].values()), 1))
