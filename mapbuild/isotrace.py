"""Trace every test in its own process, so work that only the first test to need
it triggers (lazy imports, caches built on first use) is credited to each test.

usage: isotrace.py <sqlglot checkout> <tests.json> <out dir> <parallel>
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

src, tests_json, out, par = os.path.abspath(sys.argv[1]), sys.argv[2], os.path.abspath(sys.argv[3]), int(sys.argv[4])
tests = json.load(open(tests_json))["tests"]
os.makedirs(out, exist_ok=True)
here = os.path.dirname(os.path.abspath(__file__))
deps = os.path.join(os.path.dirname(here), "deps")


def one(t):
    target = os.path.join(out, f"t{t['number']}.json")
    if os.path.exists(target):
        return t["number"], "cached", 0.0
    env = dict(os.environ)
    env.update(
        PYTHONPATH=os.pathsep.join([deps, here, src]),
        SKIP_INTEGRATION="0",
        COVMAP_OUT=target,
        COVMAP_ROOT=src,
        PYTHON_CPU_COUNT="3",
    )
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "covplug", "-p", "no:cacheprovider", t["id"]],
            cwd=src, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900,
        )
        tail = p.stdout.decode(errors="replace").strip().splitlines()[-1:] or [""]
        status = "ok" if " passed" in tail[0] and "failed" not in tail[0] else "BAD " + tail[0][:80]
    except subprocess.TimeoutExpired:
        status = "TIMEOUT"
    return t["number"], status, time.time() - t0


t0 = time.time()
bad = []
with ThreadPoolExecutor(par) as ex:
    for i, (n, status, dt) in enumerate(ex.map(one, tests)):
        if status not in ("ok", "cached"):
            bad.append((n, status))
            print(n, status, flush=True)
        if i % 200 == 0:
            print(f"{i}/{len(tests)} {time.time() - t0:.0f}s", flush=True)
print("done", len(tests), "bad", len(bad), f"{time.time() - t0:.0f}s")
