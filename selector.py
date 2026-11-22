"""Blast Radius selector.

For every commit:
  1. Static analysis (blast/analyze.py) turns the patch into the set of tests
     that could notice it, using maps built from the pinned sqlglot commit.
  2. When it is worth the CPU, a few of those tests are actually run against
     the patched code (blast/policy.py chooses which, blast/runner.py and
     blast/hot.py run them) and the result decides what is kept.
  3. The kept tests are split across workers to minimise busiest worker + 2 s
     per worker.

Answers go to stdout, one JSON line per commit. Logs go to stderr.
"""

import json
import os
import shutil
import sys
import time

T0 = time.monotonic()
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from blast import hot, policy, runner  # noqa: E402
from blast.analyze import Analyzer, module_name  # noqa: E402

DATA = os.environ.get("BLAST_DATA", "/data")
SRC = os.environ.get("BLAST_SRC", "/opt/sqlglot")
MAPDIR = os.environ.get("BLAST_MAP", os.path.join(HERE, "map"))
WORK = os.environ.get("BLAST_WORK", "/tmp/bt")
WALL_BUDGET = float(os.environ.get("BLAST_WALL", "140"))
SCALE = float(os.environ.get("BLAST_SCALE", "3.5"))  # scorer CPU seconds per map-build CPU second
NO_RUN = os.environ.get("BLAST_NORUN") == "1"
NO_HOT = os.environ.get("BLAST_NOHOT") == "1"
HOT_OVERHEAD = float(os.environ.get("BLAST_HOT_OVERHEAD", "0.5"))
HOT_MIN = int(os.environ.get("BLAST_HOT_MIN", "2"))  # warm worker only pays off with a few uses
HEAVY = ("tests/test_optimizer", "tests/test_executor")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Tree:
    """A writable copy of the pinned checkout that patches are applied to."""

    KEEP = ("sqlglot", "tests")

    def __init__(self, src, work):
        self.src = src
        self.work = work
        self.ready = False
        self.saved = []

    def ensure(self):
        if self.ready:
            return
        if os.path.exists(self.work):
            shutil.rmtree(self.work, ignore_errors=True)
        os.makedirs(self.work, exist_ok=True)
        for name in sorted(os.listdir(self.src)):
            s = os.path.join(self.src, name)
            d = os.path.join(self.work, name)
            if name in self.KEEP:
                shutil.copytree(s, d, symlinks=True)
            elif os.path.isfile(s) and os.path.getsize(s) < 300_000:
                shutil.copy2(s, d)
            elif name == "sqlglotc" and os.path.isdir(s):
                shutil.copytree(s, d, symlinks=True)
        self.ready = True

    def apply(self, changed):
        self.ensure()
        for rel, text in changed.items():
            p = os.path.join(self.work, *rel.split("/"))
            if os.path.isfile(p):
                st = os.stat(p)
                with open(p, "rb") as f:
                    self.saved.append((p, f.read(), (st.st_atime_ns, st.st_mtime_ns)))
            else:
                self.saved.append((p, None, None))
            if text is None:
                if os.path.isfile(p):
                    os.remove(p)
            else:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8", newline="") as f:
                    f.write(text)

    def restore(self):
        for p, data, times in reversed(self.saved):
            try:
                if data is None:
                    if os.path.isfile(p):
                        os.remove(p)
                else:
                    with open(p, "wb") as f:
                        f.write(data)
                    os.utime(p, ns=times)
            except OSError as e:
                log("restore failed", p, repr(e))
                self.ready = False  # rebuild the copy before the next run
        self.saved = []


def main():
    with open(os.path.join(DATA, "tests.json"), encoding="utf-8") as f:
        tests = json.load(f)["tests"]
    ms = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
    tid = {t["number"]: t["id"] for t in tests}
    all_nums = set(ms)
    commits_dir = os.path.join(DATA, "commits")
    commits = sorted(f[:-6] for f in os.listdir(commits_dir) if f.endswith(".patch"))

    answers = {}
    try:
        an = Analyzer(MAPDIR, SRC, tests)
    except Exception as e:  # noqa: BLE001
        log("FATAL: cannot load maps:", repr(e))
        an = None
    cost = (an.map.cost if an else {})

    def est(n):
        return cost.get(n, ms[n] / 1000.0 / SCALE) * SCALE

    tree = Tree(SRC, WORK)
    state = {"heavy": False, "fresh_cpu": 0.0, "hot_cpu": 0.0}
    plans = []
    for c in commits:
        try:
            with open(os.path.join(commits_dir, c + ".patch"), encoding="utf-8", errors="surrogateescape", newline="") as f:
                text = f.read()
            res = an.analyze(text) if an else None
        except Exception as e:  # noqa: BLE001
            log(c, "analysis crashed:", repr(e))
            res = None
        plans.append((c, res))

    def hot_eligible(res):
        return (
            runner.HAS_FORK and not NO_HOT and not res.cold and not res.broken and bool(res.hot_lines)
            and all(t is not None and r.startswith("sqlglot/") and r.endswith(".py") and r in res.hot_lines
                    for r, t in res.changed.items())
        )

    n_hot = sum(1 for _, res in plans if res is not None and not res.static_all and res.cands and hot_eligible(res))
    worker = hot.Worker(SRC) if n_hot >= HOT_MIN else None
    log(f"{len(commits)} commits, {n_hot} eligible for the warm worker")

    def stopper(plan):
        seen = {"n": 0}

        def stop(results):
            if len(results) == seen["n"]:
                return False
            seen["n"] = len(results)
            if any(i <= -10 for i in results):
                return False
            return policy.should_stop(plan, results, ms, est)

        return stop

    def run_fresh(c, res, plan, wall):
        """Apply the patch to the scratch tree and run the chosen tests in a fork."""
        if not state["heavy"] and any(tid[n].startswith(HEAVY) for n in plan.prefix):
            state["heavy"] = True
            if runner.HAS_FORK:
                try:
                    import duckdb  # noqa: F401
                    import numpy  # noqa: F401
                    import pandas  # noqa: F401
                except Exception as e:  # noqa: BLE001
                    log("preload failed", repr(e))
        try:
            tree.apply(res.changed)
            ordered = policy.run_order(plan.prefix, tid, est)
            spec = [(n, tid[n], max(1.5, est(n) * 6 + 1.0)) for n in ordered]
            probe_files = [r for r, t in res.changed.items() if t is not None and r.endswith(".py") and r.startswith("sqlglot/")]
            probe = [module_name(r) for r in probe_files]
            results, complete, cpu = runner.run_tests(tree.work, spec, wall, probe=probe, should_stop=stopper(plan))
            state["fresh_cpu"] += cpu
            broken = [probe_files[-10 - i] for i in results if i <= -10 and -10 - i < len(probe_files)]
            return results, complete, cpu, broken
        finally:
            try:
                tree.restore()
            except Exception as e:  # noqa: BLE001
                log("restore crashed", repr(e))
                tree.ready = False

    def run_hot(c, res, plan, wall):
        files = {}
        for rel, text in res.changed.items():
            files[rel] = {
                "text": text,
                "changed": res.hot_lines[rel],
                "import_lines": [int(k) for k in an.map.implines.get(rel, {})],
            }
        ordered = policy.run_order(plan.prefix, tid, est)
        spec = [(n, tid[n], max(1.5, est(n) * 6 + 1.0)) for n in ordered]
        heavy = any(tid[n].startswith(HEAVY) for n in plan.prefix)
        status, results, complete, cpu = worker.run(files, spec, wall, should_stop=stopper(plan), heavy=heavy)
        return status, results, complete, cpu

    for c, res in plans:
        if res is None or res.static_all:
            answers[c] = set(all_nums)
            log(c, "ALL", (res.notes if res else ""))
            continue
        cands = set(res.cands) & all_nums
        if not cands:
            answers[c] = set()
            log(c, "none", res.notes)
            continue
        try:
            heavy = any(tid[n].startswith(HEAVY) for n in cands)
            only_data = all(not r.endswith(".py") for r in res.changed)
            sig = an.signatures(res, cands) if res.focus else None
            kw = dict(focused=res.focused, sig=sig, data_only=only_data, broken=res.broken)
            cold_extra = policy.P["preload"] if heavy and not state["heavy"] else 0.0
            plan = None
            use_hot = False
            if worker is not None and not worker.dead and hot_eligible(res):
                plan = policy.make_plan(cands, ms, est, overhead=HOT_OVERHEAD, **kw)
                use_hot = plan.mode == "run" and not (set(plan.prefix) & an.map.isolated)
            if not use_hot:
                plan = policy.make_plan(cands, ms, est, extra_overhead=cold_extra, **kw)
            remaining = WALL_BUDGET - (time.monotonic() - T0)
            want_run = plan.mode == "run" and not NO_RUN
            if want_run and remaining < plan.run_est * 1.5 + 8:
                log(c, "no time left to run; emitting candidates")
                want_run = False
            if not want_run:
                answers[c] = cands
                log(c, f"emit {len(cands)} cands cost={plan.emit_cost:.1f} run_est={plan.run_est:.1f} "
                       f"exp={plan.exp_cost:.1f} prefix={len(plan.prefix)} rest={len(plan.rest)}", res.notes)
                continue
            wall = min(remaining - 6, max(6.0, plan.run_est * 4 + 6))
            t1 = time.monotonic()
            how = "fresh"
            results = None
            broken = []
            if use_hot:
                status, results, complete, cpu = run_hot(c, res, plan, wall)
                how = "hot"
                if status != "ok":
                    log(c, f"warm worker said {status}; falling back to a fresh import")
                    results = None
                    plan = policy.make_plan(cands, ms, est, extra_overhead=cold_extra, **kw)
                    if plan.mode != "run":
                        answers[c] = cands
                        log(c, f"emit {len(cands)} cands cost={plan.emit_cost:.1f} (after fallback)", res.notes)
                        continue
            if results is None:
                how = "fresh"
                results, complete, cpu, broken = run_fresh(c, res, plan, wall)
            answer = policy.finalize(plan, results, est)
            if answer is None:
                answer = set(all_nums)
            for r in broken:
                answer |= an.map.importer_tests(r) & all_nums
            if broken:
                answer |= plan.cands - {n for n in plan.prefix if results.get(n) == "P"}
            hard = sum(1 for n in plan.prefix if results.get(n) == "F")
            log(c, f"{how} ran {sum(1 for n in plan.prefix if n in results)}/{len(plan.prefix)} of {len(cands)} "
                   f"wall={time.monotonic() - t1:.2f}s cpu={cpu:.2f}s est={plan.run_est:.1f} failed={hard} "
                   f"focused={len(plan.focused)} emit={len(answer)} complete={complete} broken={broken}", res.notes)
            answers[c] = answer
        except Exception as e:  # noqa: BLE001
            log(c, "run crashed:", repr(e))
            answers[c] = cands

    if worker is not None:
        try:
            state["hot_cpu"] = worker.close()
        except Exception as e:  # noqa: BLE001
            log("worker close failed", repr(e))

    out = []
    total = 0
    for c in commits:
        sel = answers.get(c)
        if sel is None:
            sel = set(all_nums)
        cost_ms, workers = policy.split(sel, ms)
        total += cost_ms
        out.append(json.dumps({"commit": c, "workers": workers}, separators=(",", ":")))
    log(f"simulated {total / 1000:.1f}s over {len(commits)} commits; wall {time.monotonic() - T0:.1f}s "
        f"parent_cpu {time.process_time():.2f}s fresh_cpu {state['fresh_cpu']:.2f}s hot_cpu {state['hot_cpu']:.2f}s")
    return "\n".join(out) + ("\n" if out else "")


if __name__ == "__main__":
    # Keep the real stdout private so nothing but answers can reach it.
    real = os.dup(1)
    os.dup2(2, 1)
    os.environ["BLAST_REAL_FD"] = str(real)
    try:
        payload = main()
    except BaseException as e:  # noqa: BLE001
        log("FATAL", repr(e))
        raise
    data = payload.encode("utf-8")
    while data:
        data = data[os.write(real, data):]
    os.close(real)
