"""pytest plugin around covcore: writes $COVMAP_OUT plus .child.<pid>.json files."""

import json
import os
import time

import pytest

import covcore

outcome: dict = {}
durs: dict = {}


def pytest_configure(config):
    os.environ["COVMAP_PARENT"] = str(os.getpid())
    os.environ["COVMAP_CHILD"] = "1"
    with covcore._orig_open(covcore.OUT + ".cur", "w") as f:
        f.write("<collect>")
    covcore.install()
    from concurrent.futures import ProcessPoolExecutor

    orig_submit = ProcessPoolExecutor.submit

    def submit(self, fn, /, *args, **kwargs):
        return orig_submit(self, covcore.traced_call, covcore.current, fn, args, kwargs)

    ProcessPoolExecutor.submit = submit


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    with covcore._orig_open(covcore.OUT + ".cur", "w") as f:
        f.write(item.nodeid)
    covcore.set_current(item.nodeid)
    t = time.perf_counter()
    yield
    durs[item.nodeid] = time.perf_counter() - t
    covcore.set_current("<between>")


def pytest_runtest_logreport(report):
    o = outcome.get(report.nodeid)
    if report.failed:
        outcome[report.nodeid] = "failed"
    elif o is None and report.when == "call":
        outcome[report.nodeid] = report.outcome
    elif o is None and report.skipped:
        outcome[report.nodeid] = "skipped"


def pytest_unconfigure(config):
    covcore.mon.set_events(covcore.TOOL, 0)
    covcore.dump(covcore.OUT)
    with covcore._orig_open(covcore.OUT + ".meta.json", "w") as f:
        json.dump({"outcome": outcome, "dur": durs}, f)
