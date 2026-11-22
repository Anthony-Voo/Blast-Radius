"""Warm worker: verify a change without re-importing sqlglot.

A long-lived process imports the pinned sqlglot once. For a patch that only
rewrites lines inside function bodies, a forked child compiles the patched
module, swaps the new code objects into the live functions, and runs the
chosen tests. Nothing else about the process differs from a fresh import as
long as the changed functions never ran while modules were being imported;
the caller passes the lines that did (from the pinned-commit map) and the swap
is refused if a changed function touches one.

Anything this module is not sure about raises Unpatchable and the caller falls
back to a fresh import of a patched tree.
"""

import gc
import importlib
import json
import os
import sys
import types

from . import runner

CO_NEWLOCALS = 0x2


class Unpatchable(Exception):
    pass


def preload(src):
    os.chdir(src)
    if src not in sys.path:
        sys.path.insert(0, src)
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    importlib.import_module("sqlglot")
    for dp, dn, fn in os.walk(os.path.join(src, "sqlglot")):
        dn[:] = sorted(d for d in dn if d != "__pycache__")
        for f in sorted(fn):
            if not f.endswith(".py") or f == "__main__.py":
                continue
            rel = os.path.relpath(os.path.join(dp, f), src)
            mod = rel[:-3].replace(os.sep, ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            try:
                importlib.import_module(mod)
            except BaseException:  # noqa: BLE001
                pass


def _is_func(code):
    return bool(code.co_flags & CO_NEWLOCALS)


def _kids(code):
    return [c for c in code.co_consts if isinstance(c, types.CodeType)]


def _plain(consts):
    out = []
    for c in consts:
        if isinstance(c, types.CodeType):
            continue
        out.append((type(c).__name__, repr(c)))
    return out


def _same_shell(a, b):
    """Same behaviour ignoring nested code objects and line numbers."""
    return (
        a.co_code == b.co_code
        and a.co_names == b.co_names
        and a.co_varnames == b.co_varnames
        and a.co_freevars == b.co_freevars
        and a.co_cellvars == b.co_cellvars
        and a.co_argcount == b.co_argcount
        and a.co_posonlyargcount == b.co_posonlyargcount
        and a.co_kwonlyargcount == b.co_kwonlyargcount
        and a.co_flags == b.co_flags
        and a.co_name == b.co_name
        and a.co_qualname == b.co_qualname
        and a.co_exceptiontable == b.co_exceptiontable
        and _plain(a.co_consts) == _plain(b.co_consts)
    )


def _lines(code):
    return {ln for _, _, ln in code.co_lines() if ln}


def _body_lines(code):
    """(lines holding the function's own instructions, line of its header).

    The header line (where `def` or `lambda` sits) is executed by the enclosing
    scope at import time, so it says nothing about whether the body ran. Every
    instruction up to the first RESUME is set-up attributed to that line."""
    import dis

    header = code.co_firstlineno
    body = set()
    started = False
    for ins in dis.get_instructions(code):
        ln = ins.positions.lineno if ins.positions else None
        if not started:
            if ins.opname == "RESUME":
                started = True
                if ln is not None:
                    header = ln
            continue
        if ln is not None:
            body.add(ln)
    return body, header


def _pair(old, new, import_lines, swaps):
    """Walk two code trees in step. Returns True if they differ anywhere."""
    ko, kn = _kids(old), _kids(new)
    if len(ko) != len(kn) or any(a.co_name != b.co_name for a, b in zip(ko, kn)):
        raise Unpatchable("code structure changed")
    differs = False
    for a, b in zip(ko, kn):
        differs |= _pair(a, b, import_lines, swaps)
    if not _same_shell(old, new):
        if not _is_func(old):
            raise Unpatchable("import-time code changed")
        body, header = _body_lines(old)
        if header in body or old.co_firstlineno in body:
            raise Unpatchable("body shares a line with import-time code")
        if body & import_lines:
            raise Unpatchable("changed function runs during imports")
        differs = True
    if differs and _is_func(old):
        swaps.append((old, new))
    return differs


def _roots(code, out):
    """Function code objects created directly by module/class bodies."""
    for k in _kids(code):
        if _is_func(k):
            out.append(k)
        else:
            _roots(k, out)


def apply_patch(src, rel, new_text, changed_lines, import_lines):
    """Swap the patched code of sqlglot file `rel` into the live process."""
    filename = os.path.join(src, *rel.split("/"))
    target = os.path.normcase(os.path.abspath(filename))
    new_top = compile(new_text, filename, "exec", dont_inherit=True)
    roots = []
    _roots(new_top, roots)
    live = {}
    names = {}
    for obj in gc.get_objects():
        if type(obj) is types.FunctionType:
            code = obj.__code__
            fn = code.co_filename
            ok = names.get(fn)
            if ok is None:
                ok = names[fn] = os.path.normcase(os.path.abspath(fn)) == target
            if ok:
                live.setdefault((code.co_qualname, code.co_firstlineno), []).append(obj)
    import_lines = set(import_lines)
    changed_lines = set(changed_lines)
    swaps = []
    touched = False
    explained = set()
    for new in roots:
        span = _lines(new) | {new.co_firstlineno}
        for k in _all_codes(new):
            span |= _lines(k)
        relevant = bool(changed_lines & set(range(min(span), max(span) + 1)))
        objs = live.get((new.co_qualname, new.co_firstlineno))
        if not objs:
            if relevant:
                raise Unpatchable(f"no live function for {new.co_qualname}")
            continue
        if not relevant:
            continue
        if _pair(objs[0].__code__, new, import_lines, swaps):
            touched = True
            explained |= changed_lines & set(range(min(span), max(span) + 1))
    if not touched or not swaps:
        raise Unpatchable("no function body differs")
    if changed_lines - explained:
        raise Unpatchable("a changed line is outside every changed function")
    count = 0
    for old, new in swaps:
        for obj in live.get((old.co_qualname, old.co_firstlineno), ()):
            try:
                obj.__code__ = new
            except (ValueError, TypeError) as e:
                raise Unpatchable(f"cannot swap {old.co_qualname}: {e}") from None
            count += 1
    if not count:
        raise Unpatchable("nothing swapped")
    return count


def _all_codes(code):
    out = []
    for k in _kids(code):
        out.append(k)
        out.extend(_all_codes(k))
    return out


def handle(src, request, emit_line):
    """Run inside the forked child: patch, then run the tests."""
    try:
        for rel, item in request["files"].items():
            apply_patch(src, rel, item["text"], item["changed"], item["import_lines"])
    except Unpatchable as e:
        emit_line(f"X {e}")
        return
    except BaseException as e:  # noqa: BLE001
        emit_line(f"X error {e!r}")
        return
    emit_line("OK")

    def emit(index, status):
        emit_line(f"{index} {status}")

    runner.child_main(src, {"tests": request["tests"], "probe": []}, emit)


def _read_request(fd):
    head = b""
    while not head.endswith(b"\n"):
        c = os.read(fd, 1)
        if not c:
            return None
        head += c
    size = int(head)
    data = b""
    while len(data) < size:
        chunk = os.read(fd, size - len(data))
        if not chunk:
            return None
        data += chunk
    return json.loads(data.decode("utf-8"))


def serve(src, req_fd, resp_fd):
    """The warm worker's main loop (runs in a forked process)."""
    preload(src)
    heavy = False
    os.write(resp_fd, b"READY\n")
    while True:
        request = _read_request(req_fd)
        if request is None:
            return
        if request.get("heavy") and not heavy:
            heavy = True
            try:
                import duckdb  # noqa: F401
                import numpy  # noqa: F401
                import pandas  # noqa: F401
            except Exception:  # noqa: BLE001
                pass
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                try:
                    os.setsid()
                except Exception:  # noqa: BLE001
                    pass
                os.write(resp_fd, f"PID {os.getpid()}\n".encode())
                handle(src, request, lambda s: os.write(resp_fd, (s + "\n").encode()))
            except BaseException:  # noqa: BLE001
                code = 1
            finally:
                os._exit(code)
        cpu = 0.0
        try:
            _, _, ru = os.wait4(pid, 0)
            cpu = ru.ru_utime + ru.ru_stime
        except Exception:  # noqa: BLE001
            pass
        os.write(resp_fd, f"E {cpu:.3f}\n".encode())


class Worker:
    """Parent-side handle on the warm worker."""

    def __init__(self, src):
        self.src = src
        self.pid = None
        self.dead = not runner.HAS_FORK
        self.buf = b""

    def start(self):
        req_r, req_w = os.pipe()
        resp_r, resp_w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(req_w)
                os.close(resp_r)
                runner.close_answers_fd()
                dn = os.open(os.devnull, os.O_RDWR)
                os.dup2(dn, 0)
                os.dup2(dn, 1)
                os.dup2(dn, 2)
                serve(self.src, req_r, resp_w)
            except BaseException:  # noqa: BLE001
                pass
            finally:
                os._exit(0)
        os.close(req_r)
        os.close(resp_w)
        self.pid = pid
        self.req = req_w
        self.resp = resp_r
        line = self._line(30.0)
        if line != "READY":
            self.dead = True

    def _line(self, timeout):
        """One full line from the worker, or None if none arrives in time."""
        import select
        import time

        end = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = end - time.monotonic()
            if left <= 0:
                return None
            ready, _, _ = select.select([self.resp], [], [], min(left, 0.05))
            if ready:
                chunk = os.read(self.resp, 65536)
                if not chunk:
                    self.dead = True
                    return None
                self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace")

    def run(self, files, tests, wall_limit, should_stop=None, heavy=False, rss_limit_mb=1800.0):
        """Returns (status, results, complete, cpu). status is "ok", "unpatchable" or "dead"."""
        import signal
        import time

        if self.dead:
            return "dead", {}, False, 0.0
        if self.pid is None:
            self.start()
            if self.dead:
                return "dead", {}, False, 0.0
        payload = json.dumps({"files": files, "tests": [list(t) for t in tests], "heavy": heavy}).encode("utf-8")
        try:
            data = str(len(payload)).encode() + b"\n" + payload
            while data:
                data = data[os.write(self.req, data):]
        except OSError:
            self.dead = True
            return "dead", {}, False, 0.0
        deadline = time.monotonic() + wall_limit
        results = {}
        child = None
        status = "ok"
        done = False
        stopped = False
        cpu = 0.0
        killed = False
        while True:
            line = self._line(0.05)
            if line is None and self.dead:
                return "dead", results, False, cpu
            if line:
                parts = line.split()
                if parts[0] == "E":
                    try:
                        cpu = float(parts[1])
                    except (IndexError, ValueError):
                        pass
                    break
                if parts[0] == "PID":
                    child = int(parts[1])
                elif parts[0] == "X":
                    status = "unpatchable"
                elif parts[0] == "OK":
                    pass
                elif len(parts) == 2:
                    try:
                        idx = int(parts[0])
                    except ValueError:
                        continue
                    if idx == -1:
                        done = True
                    elif parts[1] == "F" or idx not in results:
                        results[idx] = parts[1]
                    if not killed and should_stop is not None and not done and should_stop(results):
                        stopped = True
                if not (stopped and not killed):
                    continue
            over = time.monotonic() > deadline
            if child and not killed and (over or stopped or runner._rss_mb(child) > rss_limit_mb):
                stopped = True
                killed = True
                for kill in (getattr(os, "killpg", None), os.kill):
                    try:
                        if kill is not None:
                            kill(child, signal.SIGKILL)
                    except OSError:
                        pass
            if over and time.monotonic() > deadline + 15:
                self.dead = True
                return "dead", results, False, cpu
        return status, results, (done and not stopped), cpu

    def close(self):
        if self.pid is None:
            return 0.0
        import signal

        cpu = 0.0
        try:
            os.close(self.req)
        except OSError:
            pass
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            _, _, ru = os.wait4(self.pid, 0)
            cpu = ru.ru_utime + ru.ru_stime
        except Exception:  # noqa: BLE001
            pass
        self.pid = None
        return cpu


def dev_main(src, req_path, out_path):
    """Development helper (no fork needed): preload, patch, run, write results."""
    src, req_path, out_path = (os.path.abspath(a) for a in (src, req_path, out_path))
    with open(req_path, encoding="utf-8") as f:
        request = json.load(f)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    preload(src)
    handle(src, request, lambda line: os.write(fd, (line + "\n").encode()))
