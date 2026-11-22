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
data_imports: dict = {}  # file -> line -> set of modules whose import executed it
data_class_imports: dict = {}  # class -> modules whose import instantiated it
import_stack: list = []
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
    del import_stack[:]
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
        if import_stack:
            top = import_stack[-1]
            if top != rel or code.co_name != "<module>":
                data_imports.setdefault(rel, {}).setdefault(line, set()).add(top)
    return mon.DISABLE


def _py_start(code, offset):
    if code.co_name == "<module>":
        rel = _rel(code.co_filename)
        if rel:
            import_stack.append(rel)
            mon.restart_events()
            return None
    return mon.DISABLE


def _py_return(code, offset, retval):
    if code.co_name == "<module>":
        rel = _rel(code.co_filename)
        if rel:
            if import_stack and import_stack[-1] == rel:
                import_stack.pop()
            elif rel in import_stack:
                del import_stack[import_stack.index(rel):]
            ensure_wrapped()
            mon.restart_events()
            return None
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
        top = import_stack[-1] if import_stack else None
        key = (t, top)
        if key not in _seen_types:
            _seen_types.add(key)
            names = []
            for c in t.__mro__:
                m = getattr(c, "__module__", "") or ""
                if m.startswith("sqlglot"):
                    names.append(f"{m}:{c.__qualname__}")
            data_classes.setdefault(current, set()).update(names)
            if top:
                for nm in names:
                    data_class_imports.setdefault(nm, set()).add(top)
        return orig(self, *a, **k)

    cls.__init__ = __init__


_TARGETS = [
    ("sqlglot.expressions.core", "Expr"),
    ("sqlglot.tokens", "Tokenizer"),
    ("sqlglot.parser", "Parser"),
    ("sqlglot.generator", "Generator"),
    ("sqlglot.dialects.dialect", "Dialect"),
]
_done_targets = set()


def ensure_wrapped():
    if len(_done_targets) == len(_TARGETS):
        return
    for modname, clsname in _TARGETS:
        if (modname, clsname) in _done_targets:
            continue
        mod = sys.modules.get(modname)
        cls = getattr(mod, clsname, None) if mod is not None else None
        if cls is not None:
            _done_targets.add((modname, clsname))
            try:
                _wrap_init(cls)
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
    mon.register_callback(TOOL, mon.events.PY_START, _py_start)
    mon.register_callback(TOOL, mon.events.PY_RETURN, _py_return)
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START | mon.events.PY_RETURN)


def dump(path):
    out = {
        "lines": {t: {f: sorted(ls) for f, ls in d.items()} for t, d in data_lines.items()},
        "files": {t: sorted(s) for t, s in data_files.items()},
        "classes": {t: sorted(s) for t, s in data_classes.items()},
        "imports": {f: {str(l): sorted(m) for l, m in d.items()} for f, d in data_imports.items()},
        "class_imports": {c: sorted(m) for c, m in data_class_imports.items()},
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
