"""Cost model: when to verify candidates by running them, and what to emit.

All functions are pure so the same code drives the selector and the offline
simulator used to tune the constants.

Costs are in seconds. `ms[n]` is a test's recorded duration in milliseconds
(what emitting it costs); `est(n)` is the CPU seconds we expect to burn by
running it ourselves.

Evidence: when the change is inside a function body, every candidate has a
"signature" - the lines of that function it executes. A few cheap tests are
chosen so that together they execute every line any candidate executes there.
An unrun test is then
  * kept, if some failing test's signature is a subset of its own (it walks the
    same path and more), or if passing tests do not cover all of its lines;
  * dropped, if every line it executes in the function was executed by a test
    that passed.
"""

import os

WORKER_MS = 2000
MAX_WORKERS = 8


def _f(name, default):
    return float(os.environ.get(name, default))


P = {
    "overhead": _f("BLAST_OVERHEAD", "1.5"),  # CPU to fork, import sqlglot and the test modules
    "preload": _f("BLAST_PRELOAD", "1.5"),  # one-off import of pandas/duckdb
    "budget": _f("BLAST_BUDGET", "3.0"),  # CPU seconds of cheapest-first tests run as evidence
    "cover_budget": _f("BLAST_COVER_BUDGET", "5.0"),  # CPU seconds the path-covering tests may use
    "full": _f("BLAST_FULL", "6.0"),  # candidate sets cheaper than this are run completely
    "p_fail": _f("BLAST_PFAIL", "0.5"),  # prior that a code change breaks some candidate
    "p_fail_data": _f("BLAST_PFAIL_DATA", "0.9"),  # same for fixture-only changes
    "margin": _f("BLAST_MARGIN", "0.3"),
    "giant_est": _f("BLAST_GIANT", "7.0"),  # tests dearer than this to run are "slow"
    "wide_cands": _f("BLAST_WIDE_CANDS", "300"),  # candidate sets this large ...
    "wide_frac": _f("BLAST_WIDE_FRAC", "0.4"),
    "wide_probe": _f("BLAST_WIDE_PROBE", "30"),  # cheap tests spot-checked when a large set is emitted unverified  # ... with this share of run tests failing mean "everything is broken"
    "clean_rule": _f("BLAST_CLEAN_RULE", "3"),  # with clean evidence clear: 0 path-covered tests; 1 those and slow ones; 2 all; 3 slow ones only
    "nonslow_full": _f("BLAST_NONSLOW_FULL", "8.0"),  # run every non-slow candidate when that costs no more than this
    "rare_frac": _f("BLAST_RARE_FRAC", "0"),  # experimental, off: drop slow tests when under this share of evidence fails
    "min_evidence": _f("BLAST_MIN_EVIDENCE", "10"),  # fewer run tests than this is not evidence about the rest
}


def split(nums, ms):
    """Best split found over 1..8 workers: (cost_ms, workers)."""
    if not nums:
        return 0, []
    order = sorted(nums, key=lambda n: (-ms[n], n))
    best = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads = [0] * k
        buckets = [[] for _ in range(k)]
        for n in order:
            i = loads.index(min(loads))
            loads[i] += ms[n]
            buckets[i].append(n)
        cost_ms = max(loads) + WORKER_MS * k
        if best is None or cost_ms < best[0]:
            best = (cost_ms, buckets)
        if max(loads) == ms[order[0]]:
            break  # the longest test already bounds the busiest worker
    return best[0], [sorted(b) for b in best[1] if b]


def cost(nums, ms):
    return split(nums, ms)[0] / 1000.0


class Plan:
    __slots__ = ("mode", "prefix", "rest", "run_est", "emit_cost", "exp_cost", "focused", "sig", "cands", "keep_all", "pinned")

    def __init__(self):
        self.mode = "emit"
        self.prefix = []
        self.rest = set()
        self.run_est = 0.0
        self.emit_cost = 0.0
        self.exp_cost = 0.0
        self.focused = set()
        self.sig = {}
        self.cands = set()
        self.keep_all = False  # the run only checks for widespread breakage; nothing unrun is dropped
        self.pinned = set()  # tests that read a changed file: never dropped without being run


def make_plan(cands, ms, est, focused=(), sig=None, data_only=False, broken=False, extra_overhead=0.0,
              overhead=None, pinned=(), p=P):
    """Decide between emitting every candidate and running a chosen prefix."""
    plan = Plan()
    cands = set(cands)
    plan.cands = cands
    plan.pinned = set(pinned) & cands
    plan.emit_cost = cost(cands, ms)
    plan.sig = sig or {}
    plan.focused = (set(focused) & cands) if sig else set()
    order = sorted(cands, key=lambda n: (est(n), n))
    total = sum(est(n) for n in order)
    chosen = []
    cum = 0.0
    nonslow = [n for n in order if est(n) <= p["giant_est"]]
    nonslow_total = sum(est(n) for n in nonslow)
    if total <= p["full"]:
        chosen = list(order)
        cum = total
    elif nonslow and nonslow_total <= p["nonslow_full"]:
        chosen = nonslow
        cum = nonslow_total
    else:
        taken = set()
        if plan.focused:
            pool = [n for n in order if n in plan.focused and est(n) <= p["cover_budget"]]
            universe = set()
            for n in pool:
                universe |= plan.sig[n]
            covered = set()
            while len(covered) < len(universe):
                best = None
                best_score = 0.0
                for n in pool:
                    if n in taken:
                        continue
                    gain = len(plan.sig[n] - covered)
                    if gain:
                        score = gain / max(est(n), 0.02)
                        if score > best_score:
                            best_score = score
                            best = n
                if best is None or cum + est(best) > p["cover_budget"]:
                    break
                taken.add(best)
                chosen.append(best)
                covered |= plan.sig[best]
                cum += est(best)
        for n in order:
            if n in taken:
                continue
            e = est(n)
            if cum + e > p["budget"]:
                break
            taken.add(n)
            chosen.append(n)
            cum += e
    plan.prefix = chosen
    plan.rest = cands - set(chosen)
    plan.run_est = cum + (p["overhead"] if overhead is None else overhead) + extra_overhead
    if not chosen:
        return plan
    pf = p["p_fail_data"] if data_only else p["p_fail"]
    clean_cost = cost(finalize(plan, {n: "P" for n in chosen}, est, p) or (), ms)
    if plan.rest:
        slow = {n for n in plan.rest if est(n) > p["giant_est"]} - plan.pinned
        fail_cost = 0.5 * plan.emit_cost + 0.5 * cost(cands - slow, ms)
    else:
        fail_cost = min(plan.emit_cost, 2.6)
    plan.exp_cost = plan.run_est + pf * fail_cost + (1 - pf) * clean_cost
    if broken or plan.exp_cost + p["margin"] < plan.emit_cost:
        plan.mode = "run"
    elif len(cands) >= p["wide_cands"]:
        # Not worth verifying, but a change this widely covered may break everything,
        # including tests outside the candidates: spot-check a few cheap tests.
        probe = [n for n in order if est(n) <= 0.5][: int(p["wide_probe"])]
        if len(probe) >= 10:
            plan.mode = "run"
            plan.keep_all = True
            plan.prefix = probe
            plan.rest = cands - set(probe)
            plan.run_est = sum(est(n) for n in probe) + (p["overhead"] if overhead is None else overhead) + extra_overhead
    return plan


def finalize(plan, results, est, p=P):
    """The tests to emit given the results that came back."""
    ran = [n for n in plan.prefix if n in results]
    failed = [n for n in ran if results[n] != "P"]
    keep = set(failed)
    focused = plan.focused
    sig = plan.sig
    passed_union = set()
    for n in ran:
        if results[n] == "P" and n in focused:
            passed_union |= sig[n]
    failed_sigs = []
    for n in failed:
        if n in focused:
            fs = sig[n]
            if not any(o <= fs for o in failed_sigs):
                failed_sigs = [o for o in failed_sigs if not fs <= o] + [fs]
    strong = len(ran) >= p["min_evidence"]
    if len(plan.cands) >= p["wide_cands"] and len(failed) >= 3 and len(failed) >= p["wide_frac"] * len(ran):
        # Widespread breakage also takes down tests that never touch the changed
        # code (shared caches, modules that no longer import): select everything.
        return None
    if plan.keep_all:
        return plan.cands - {n for n in ran if results[n] == "P"}
    for n in plan.cands:
        if n in results:
            continue
        if n in plan.pinned:
            # It reads a file the patch changed; other tests passing says nothing about it.
            keep.add(n)
            continue
        if failed:
            # Something broke. Whether an unrun test hits the same problem depends
            # on its data, not just its path, so nothing unrun can be cleared
            # (unless the experimental "rare_frac" rule is switched on).
            if not (p["rare_frac"] > 0 and strong and len(ran) >= 50 and len(failed) < p["rare_frac"] * len(ran)
                    and est(n) > p["giant_est"]):
                keep.add(n)
        elif n in focused:
            # Nothing failed: clear a test once passing tests executed every
            # line it executes in the changed function.
            covered = sig[n] <= passed_union
            if p["clean_rule"] >= 3:
                ok = strong and est(n) > p["giant_est"]
            elif p["clean_rule"] >= 2:
                ok = strong
            elif p["clean_rule"] >= 1:
                ok = strong and (covered or est(n) > p["giant_est"])
            else:
                ok = strong and covered
            if not ok:
                keep.add(n)
        elif not (strong and est(n) > p["giant_est"]):
            keep.add(n)
    return keep


def should_stop(plan, results, ms, est, p=P):
    """Called as results arrive: is it cheaper to stop and emit what is left?"""
    left = [n for n in plan.prefix if n not in results]
    if not left:
        return False
    failed = [n for n in plan.prefix if results.get(n) == "F"]
    if not failed or plan.keep_all:
        return False
    if len(plan.cands) >= p["wide_cands"] and len(failed) < 3 and len(results) < 25:
        return False  # too early to tell isolated failures from widespread breakage
    now = finalize(plan, results, est, p)
    if now is None:
        return True  # everything will be selected whatever else we learn
    stop_cost = cost(now, ms)
    # Guess the outcome of carrying on: a focused test fails if it walks a path
    # that already failed; an unfocused one fails if most others did.
    sig = plan.sig
    failed_sigs = [sig[n] for n in failed if n in plan.focused]
    unfocused_bad = sum(1 for n in failed if n not in plan.focused)
    unfocused_ran = sum(1 for n in plan.prefix if n in results and n not in plan.focused)
    guess = dict(results)
    for n in left:
        if n in plan.focused:
            guess[n] = "F" if any(fs <= sig[n] for fs in failed_sigs) else "P"
        else:
            guess[n] = "F" if unfocused_ran and unfocused_bad * 2 >= unfocused_ran else "P"
    later = finalize(plan, guess, est, p)
    go_cost = sum(est(n) for n in left) + cost(plan.cands if later is None else later, ms)
    return stop_cost <= go_cost + 0.05


def run_order(prefix, tid, est):
    """Cheap classes first; tests of one class stay together (one setUpClass)."""
    first = {}
    for n in prefix:
        k = tid[n].rsplit("::", 1)[0]
        e = est(n)
        if k not in first or e < first[k]:
            first[k] = e
    return sorted(prefix, key=lambda n: (first[tid[n].rsplit("::", 1)[0]], tid[n].rsplit("::", 1)[0], est(n), n))
