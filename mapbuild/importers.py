"""For each test file: which sqlglot/tests .py files are loaded just by importing it."""
import json, os, subprocess, sys
root = os.path.realpath(sys.argv[1])
tests = json.load(open(sys.argv[2]))["tests"]
files = sorted({t["file"] for t in tests})
code = r'''
import sys, os, json, importlib
root=os.path.realpath(sys.argv[1]); mod=sys.argv[2]
sys.path.insert(0, root); os.chdir(root)
importlib.import_module(mod)
out=[]
for m in list(sys.modules.values()):
    f=getattr(m,"__file__",None)
    if f:
        f=os.path.realpath(f)
        if f.startswith(root+os.sep): out.append(os.path.relpath(f,root).replace(os.sep,"/"))
print(json.dumps(sorted(out)))
'''
res = {}
procs = []
for f in files:
    mod = f[:-3].replace("/", ".")
    procs.append((f, subprocess.Popen([sys.executable, "-c", code, root, mod], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)))
for f, p in procs:
    out, _ = p.communicate()
    res[f] = json.loads(out.decode().strip().splitlines()[-1])
json.dump(res, open(sys.argv[3], "w"))
print(len(res), "test files;", sum(len(v) for v in res.values()), "edges")
