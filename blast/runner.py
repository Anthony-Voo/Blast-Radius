"""Runs chosen sqlglot tests against a patched source tree and reports which fail.

The tests are plain unittest.TestCase classes, so they are run with unittest
directly (no pytest start-up cost). Process pools used by a few tests are
replaced with an in-process pool so no worker interpreters are spawned.

The child reports one line per test: "<index> <P|F>\n". A test with no line
is "unknown" to the parent, which must then treat it as failing.
"""

import importlib
import json
import os
import pickle
import sys
import time
import unittest

HAS_FORK = hasattr(os, "fork")
TIMES = {}


class TestTimeout(BaseException):
    pass


def _install_inline_pool():
    import concurrent.futures as cf
    import concurrent.futures.process as cfp
    from concurrent.futures import Future

    class InlinePool:
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
            self._initializer = initializer
            self._initargs = initargs
            self._inited = False

        def submit(self, fn, /, *args, **kwargs):
            fut = Future()
            try:
                if not self._inited:
                    self._inited = True
                    if self._initializer is not None:
                        self._initializer(*self._initargs)
                args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
                result = fn(*args, **kwargs)
                result = pickle.loads(pickle.dumps(result))
            except TestTimeout:
                raise
            except BaseException as e:  # noqa: BLE001
                fut.set_exception(e)
            else:
                fut.set_result(result)
            return fut

        def map(self, fn, *iterables, timeout=None, chunksize=1):
            futs = [self.submit(fn, *a) for a in zip(*iterables)]
            return (f.result() for f in futs)

        def shutdown(self, wait=True, *, cancel_futures=False):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    cf.ProcessPoolExecutor = InlinePool
    cfp.ProcessPoolExecutor = InlinePool


class _Result(unittest.TestResult):
    def __init__(self, emit, index_of, timeouts):
        super().__init__()
        self.emit = emit
        self.index_of = index_of  # method name -> test index
        self.timeouts = timeouts
        self.bad = set()
        self.holder_failed = False
        self._timer = None
        try:
            import signal

            if hasattr(signal, "setitimer") and hasattr(signal, "SIGALRM"):
                self._timer = signal
        except Exception:
            self._timer = None

    def _mark(self, test):
        sub = getattr(test, "test_case", None)
        if sub is not None:
            test = sub
        name = getattr(test, "_testMethodName", None)
        if isinstance(test, unittest.TestCase) and name in self.index_of:
            self.bad.add(name)
        else:
            self.holder_failed = True

    def addError(self, test, err):
        self._mark(test)

    def addFailure(self, test, err):
        self._mark(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test)

    def addUnexpectedSuccess(self, test):
        self._mark(test)

    def addSkip(self, test, reason):
        pass

    def addExpectedFailure(self, test, err):
        pass

    def addSuccess(self, test):
        pass

    def startTest(self, test):
        self.testsRun += 1
        self._t0 = time.process_time()
        if self._timer is not None:
            idx = self.index_of.get(getattr(test, "_testMethodName", None))
            limit = self.timeouts.get(idx, 20.0) if idx is not None else 20.0
            self._timer.setitimer(self._timer.ITIMER_REAL, limit)

    def stopTest(self, test):
        if self._timer is not None:
            self._timer.setitimer(self._timer.ITIMER_REAL, 0)
        name = getattr(test, "_testMethodName", None)
        idx = self.index_of.get(name)
        if idx is not None:
            TIMES[idx] = time.process_time() - self._t0
            self.emit(idx, "F" if name in self.bad else "P")


def _alarm(signum, frame):
    raise TestTimeout()


def child_main(tree, spec, emit):
    """spec: {"tests": [[index, id, timeout_seconds], ...]} in suite order."""
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ.pop("PYTHONPATH", None)
    os.chdir(tree)
    sys.path.insert(0, tree)
    try:
        import signal

        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, _alarm)
    except Exception:
        pass
    _install_inline_pool()

    for i, modname in enumerate(spec.get("probe", ())):
        try:
            importlib.import_module(modname)
        except BaseException:  # noqa: BLE001
            emit(-10 - i, "F")

    groups = []  # [(file, cls, [(index, method, timeout)])]
    for index, tid, limit in spec["tests"]:
        file, cls, method = tid.split("::")
        if groups and groups[-1][0] == file and groups[-1][1] == cls:
            groups[-1][2].append((index, method, limit))
        else:
            groups.append((file, cls, [(index, method, limit)]))

    modules = {}
    for file, cls_name, items in groups:
        if file not in modules:
            try:
                modules[file] = importlib.import_module(file[:-3].replace("/", "."))
            except BaseException:  # noqa: BLE001  (import errors fail every test in the file)
                modules[file] = None
        mod = modules[file]
        cls = getattr(mod, cls_name, None) if mod is not None else None
        if cls is None:
            for index, _, _ in items:
                emit(index, "F")
            continue
        index_of = {}
        timeouts = {}
        cases = []
        for index, method, limit in items:
            try:
                case = cls(method)
            except BaseException:  # noqa: BLE001
                emit(index, "F")
                continue
            index_of[method] = index
            timeouts[index] = limit
            cases.append(case)
        if not cases:
            continue
        result = _Result(emit, index_of, timeouts)
        try:
            unittest.TestSuite(cases).run(result)
        except BaseException:  # noqa: BLE001
            result.holder_failed = True
        if result.holder_failed:
            for index in index_of.values():
                emit(index, "F")
    emit(-1, "D")


def close_answers_fd():
    """Children must not keep the selector's private stdout open."""
    fd = os.environ.get("BLAST_REAL_FD")
    if fd:
        try:
            os.close(int(fd))
        except (OSError, ValueError):
            pass


def _fd_emitter(fd):
    def emit(index, status):
        os.write(fd, f"{index} {status}\n".encode())

    return emit


def _parse(buf, results):
    done = False
    for line in buf.split(b"\n"):
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        st = parts[1].decode()
        if idx == -1:
            done = True
        elif st == "F" or idx not in results:
            results[idx] = st
    return done


def _rss_mb(pid):
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * 4096 / 1e6
    except Exception:
        return 0.0


def run_tests(tree, tests, wall_limit, probe=(), should_stop=None, rss_limit_mb=1800.0):
    """tests: [(index, id, timeout)]. Returns (results, complete, cpu_seconds).

    results maps index -> "P"/"F" for every test the child reported; complete
    is False when the child was stopped, killed or died before finishing.
    Probe module i that fails to import is reported under index -10 - i.
    should_stop(results) is polled as results arrive; returning True kills the
    child early (tests without a result are then unknown to the caller)."""
    spec = {"tests": [list(t) for t in tests], "probe": list(probe)}
    results = {}
    deadline = time.monotonic() + wall_limit
    stopped = False
    if HAS_FORK:
        import select
        import signal

        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                os.close(r)
                close_answers_fd()
                try:
                    os.setsid()
                except Exception:
                    pass
                dn = os.open(os.devnull, os.O_RDWR)
                os.dup2(dn, 0)
                os.dup2(dn, 1)
                os.dup2(dn, 2)
                child_main(tree, spec, _fd_emitter(w))
            except BaseException:  # noqa: BLE001
                code = 1
            finally:
                os._exit(code)
        os.close(w)
        buf = b""
        done = False
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or _rss_mb(pid) > rss_limit_mb:
                stopped = True
                break
            ready, _, _ = select.select([r], [], [], min(left, 0.05))
            if ready:
                chunk = os.read(r, 65536)
                if not chunk:
                    break
                buf += chunk
                cut = buf.rfind(b"\n")
                if cut >= 0:
                    done = _parse(buf[: cut + 1], results) or done
                    buf = buf[cut + 1 :]
                if done:
                    break
                if should_stop is not None and should_stop(results):
                    stopped = True
                    break
        for kill in (getattr(os, "killpg", None), os.kill):
            try:
                if kill is not None:
                    kill(pid, signal.SIGKILL)
            except Exception:
                pass
        os.close(r)
        cpu = 0.0
        try:
            _, _, ru = os.wait4(pid, 0)
            cpu = ru.ru_utime + ru.ru_stime
        except Exception:
            pass
        return results, (done and not stopped), cpu

    # No fork (development on Windows): same child logic in a subprocess.
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        spec_path = os.path.join(td, "spec.json")
        out_path = os.path.join(td, "out.txt")
        with open(spec_path, "w") as f:
            json.dump(spec, f)
        env = dict(os.environ, BLAST_TIMING="1")
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), tree, spec_path, out_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        done = False

        def poll():
            try:
                with open(out_path, "rb") as f:
                    data = f.read()
            except OSError:
                return False
            cut = data.rfind(b"\n")
            return _parse(data[: cut + 1], results) if cut >= 0 else False

        while True:
            exited = proc.poll() is not None
            done = poll() or done
            if exited or done:
                break
            if time.monotonic() > deadline or (should_stop is not None and should_stop(results)):
                stopped = True
                break
            try:
                time.sleep(0.05)
            except OSError:
                pass
        if proc.poll() is None:
            if not stopped:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        cpu = 0.0
        try:
            with open(out_path + ".times.json") as f:
                cpu = json.load(f)["total"]
        except (OSError, ValueError, KeyError):
            cpu = sum(TIMES.values())
    return results, (done and not stopped), cpu


if __name__ == "__main__":
    _tree, _spec_path, _out_path = (os.path.abspath(a) for a in sys.argv[1:4])
    with open(_spec_path) as _f:
        _spec = json.load(_f)
    _out = os.open(_out_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    _cpu0 = time.process_time()
    child_main(os.path.abspath(_tree), _spec, _fd_emitter(_out))
    if os.environ.get("BLAST_TIMING"):
        with open(_out_path + ".times.json", "w") as _f:
            json.dump({"times": TIMES, "total": time.process_time() - _cpu0}, _f)
