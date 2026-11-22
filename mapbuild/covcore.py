"""Shared tracer: per-test line coverage of sqlglot/, files opened, classes used.

Used by the pytest plugin (parent process) and by sitecustomize (spawned
worker processes and subprocesses), so work done in ProcessPoolExecutor
workers is attributed to the test that submitted it.
"""

import builtins
import io
import json
import os
import sys

ROOT = os.path.realpath(os.environ.get("COVMAP_ROOT", os.getcwd()))
PKGS = (os.path.join(ROOT, "sqlglot") + os.sep, os.path.join(ROOT, "tests") + os.sep)
OUT = os.environ.get("COVMAP_OUT", "covmap.json")

mon = sys.monitoring
TOOL = 3
current = "<collect>"
data_lines: dict = {}
data_files: dict = {}
data_classes: dict = {}
_seen_types: set = set()
_norm_cache: dict = {}
_orig_open = builtins.open
_orig_listdir = os.listdir
_orig_scandir = os.scandir
_wrapped = False
_installed = False


def set_current(name):
    global current, _seen_types
    current = name
    _seen_types = set()
    mon.restart_events()
    ensure_wrapped()


def _rel(path):
    r = _norm_cache.get(path)
    if r is None:
        try:
            rp = os.path.realpath(path)
        except Exception:
            rp = ""
        if rp.startswith(PKGS) and rp.endswith(".py"):
            r = os.path.relpath(rp, ROOT).replace(os.sep, "/")
        else:
            r = ""
        _norm_cache[path] = r
    return r


def _line(code, line):
    rel = _rel(code.co_filename)
    if rel:
        d = data_lines.get(current)
        if d is None:
            d = data_lines[current] = {}
        s = d.get(rel)
        if s is None:
            s = d[rel] = set()
        s.add(line)
    return mon.DISABLE


def _note_path(file, prefix=""):
    try:
        if isinstance(file, (str, os.PathLike)):
            p = os.path.realpath(os.fspath(file))
            if p == ROOT or p.startswith(ROOT + os.sep):
                rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
                if "__pycache__" not in rel:
                    data_files.setdefault(current, set()).add(prefix + rel)
    except Exception:
        pass


def _open(file, *a, **k):
    _note_path(file)
    return _orig_open(file, *a, **k)


def _listdir(path="."):
    _note_path(path, "dir:")
    return _orig_listdir(path)


def _scandir(path="."):
    _note_path(path, "dir:")
    return _orig_scandir(path)


def _wrap_init(cls):
    orig = cls.__dict__.get("__init__")
    if orig is None:
        return

    def __init__(self, *a, **k):
        t = type(self)
        if t not in _seen_types:
            _seen_types.add(t)
            s = data_classes.setdefault(current, set())
            for c in t.__mro__:
                m = getattr(c, "__module__", "") or ""
                if m.startswith("sqlglot"):
                    s.add(f"{m}:{c.__qualname__}")
        return orig(self, *a, **k)

    cls.__init__ = __init__


def ensure_wrapped():
    global _wrapped
    if _wrapped or "sqlglot.generator" not in sys.modules:
        return
    _wrapped = True
    try:
        from sqlglot.dialects.dialect import Dialect
        from sqlglot.expressions.core import Expr
        from sqlglot.generator import Generator
        from sqlglot.parser import Parser
        from sqlglot.tokens import Tokenizer

        for c in (Dialect, Parser, Generator, Tokenizer, Expr):
            _wrap_init(c)
    except Exception as e:  # pragma: no cover
        print("covcore wrap failed", repr(e), file=sys.stderr)


def install():
    global _installed
    if _installed:
        return
    _installed = True
    builtins.open = _open
    io.open = _open
    os.listdir = _listdir
    os.scandir = _scandir
    try:
        import tokenize

        tokenize._builtin_open = _open
    except Exception:
        pass
    mon.use_tool_id(TOOL, "covmap")
    mon.register_callback(TOOL, mon.events.LINE, _line)
    mon.set_events(TOOL, mon.events.LINE)


def dump(path):
    out = {
        "lines": {t: {f: sorted(ls) for f, ls in d.items()} for t, d in data_lines.items()},
        "files": {t: sorted(s) for t, s in data_files.items()},
        "classes": {t: sorted(s) for t, s in data_classes.items()},
    }
    tmp = f"{path}.tmp{os.getpid()}"
    with _orig_open(tmp, "w") as f:
        json.dump(out, f)
    os.replace(tmp, path)


def child_dump():
    dump(f"{OUT}.child.{os.getpid()}.json")


def install_child():
    try:
        with _orig_open(OUT + ".cur") as f:
            cur = f.read().strip()
    except Exception:
        cur = "<child>"
    global current
    current = cur or "<child>"
    install()
    import atexit

    atexit.register(child_dump)
    if "--multiprocessing-fork" in sys.argv or any("multiprocessing" in a for a in sys.argv):
        from multiprocessing import util

        util.Finalize(None, child_dump, exitpriority=100)


def traced_call(test_id, fn, args, kwargs):
    set_current(test_id)
    try:
        return fn(*args, **kwargs)
    finally:
        ensure_wrapped()
