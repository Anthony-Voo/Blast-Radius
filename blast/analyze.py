"""Static part: from a patch to the set of tests that could possibly notice it.

Everything here is derived from the pinned sqlglot commit: per-test line
coverage, which fixture files each test opens, which dialect/parser/generator/
expression classes each test instantiates, and which modules each test file
imports. Nothing is keyed on a patch or on a kind of edit.
"""

import ast
import gzip
import json
import os
import re
import textwrap

from . import patchlib

CODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
ROOT_CONFIG = {"pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py"}


class Map:
    def __init__(self, mapdir, tests):
        self.dir = mapdir
        with gzip.open(os.path.join(mapdir, "meta.json.gz"), "rt") as f:
            meta = json.load(f)
        self.tests = tests
        num_by_id = {t["id"]: t["number"] for t in tests}
        self.num_of_idx = [num_by_id.get(i) for i in meta["tests"]]
        known = set(meta["tests"])
        self.unknown = {t["number"] for t in tests if t["id"] not in known}
        self.cost = {}
        for i, c in zip(meta["tests"], meta["cost"]):
            n = num_by_id.get(i)
            if n is not None:
                self.cost[n] = c
        self.files = meta["files"]
        self.classes = meta["classes"]
        self.importers = meta["importers"]
        self.lined = set(meta["lined"])
        self.implines = meta.get("implines", {})
        self.classimports = meta.get("classimports", {})
        self.isolated_ids = set(meta.get("isolated", ()))
        self.isolated = {t["number"] for t in tests if t["id"] in self.isolated_ids}
        self.by_file = {}
        self.by_class = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
            f, c, _ = t["id"].split("::")
            self.by_class.setdefault((f, c), set()).add(t["number"])
        self.all = {t["number"] for t in tests}
        self._bits = {}
        self._lines = {}

    def decode(self, hexbits):
        r = self._bits.get(hexbits)
        if r is None:
            v = int(hexbits, 16)
            r = set()
            nums = self.num_of_idx
            i = 0
            while v:
                low = v & 0xFFFFFFFF
                if low:
                    for b in range(32):
                        if low >> b & 1:
                            n = nums[i + b]
                            if n is not None:
                                r.add(n)
                v >>= 32
                i += 32
            self._bits[hexbits] = r
        return r

    def _file(self, rel):
        d = self._lines.get(rel)
        if d is None:
            if rel in self.lined:
                p = os.path.join(self.dir, "lines", rel.replace("/", "__") + ".json.gz")
                with gzip.open(p, "rt") as f:
                    d = json.load(f)
            else:
                d = {"sets": [], "lines": {}}
            self._lines[rel] = d
        return d

    def line_tests(self, rel, lo, hi):
        d = self._file(rel)
        out = set()
        seen = set()
        lines = d["lines"]
        for ln in range(lo, hi + 1):
            j = lines.get(str(ln))
            if j is not None and j not in seen:
                seen.add(j)
                out |= self.decode(d["sets"][j])
        return out

    def file_tests(self, rel):
        d = self._file(rel)
        out = set()
        for s in d["sets"]:
            out |= self.decode(s)
        return out

    def openers(self, rel):
        h = self.files.get(rel)
        return set(self.decode(h)) if h else set()

    def class_users(self, key):
        h = self.classes.get(key)
        return None if h is None else set(self.decode(h))

    def importer_tests(self, rel):
        out = set()
        for tf in self.importers.get(rel, ()):
            out |= self.by_file.get(tf, set())
        return out


class Src:
    """Line-level facts about one Python source file."""

    def __init__(self, text):
        self.text = text
        self.lines = text.split("\n")
        self.tree = ast.parse(text)
        self.stmts = []  # (lineno, end, node, func, cls_qual)
        self.funcs = []  # (start(with decorators), end, node)
        self.lambdas = []  # (lineno, col, end_lineno, end_col)
        self.classes = []  # (start, end, node, qual)
        self.strspans = []  # multi-line string literals
        self._visit_body(self.tree.body, None, None)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Lambda):
                b = node.body
                self.lambdas.append((b.lineno, b.col_offset, b.end_lineno, b.end_col_offset))
            elif isinstance(node, (ast.Constant, ast.JoinedStr)):
                if getattr(node, "end_lineno", node.lineno) > node.lineno:
                    self.strspans.append((node.lineno, node.end_lineno))

    @staticmethod
    def _start(node):
        decs = getattr(node, "decorator_list", None)
        if decs:
            return min(node.lineno, min(d.lineno for d in decs))
        return node.lineno

    def _visit_body(self, body, func, qual):
        for node in body:
            self._visit_stmt(node, func, qual)

    def _visit_stmt(self, node, func, qual):
        start = self._start(node)
        self.stmts.append((start, node.end_lineno, node, func, qual))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.funcs.append((start, node.end_lineno, node))
            self._visit_body(node.body, node, None)
        elif isinstance(node, ast.ClassDef):
            q = node.name if not qual else f"{qual}.{node.name}"
            if func is not None:
                q = None
            self.classes.append((start, node.end_lineno, node, q))
            self._visit_body(node.body, func, q if func is None else None)
        else:
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(node, field, None)
                if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                    self._visit_body(sub, func, qual)
            for h in getattr(node, "handlers", ()) or ():
                self._visit_body(h.body, func, qual)
            for c in getattr(node, "cases", ()) or ():
                self._visit_body(c.body, func, qual)

    def innermost(self, line):
        best = None
        for s in self.stmts:
            if s[0] <= line <= s[1]:
                if best is None or (s[1] - s[0]) <= (best[1] - best[0]):
                    best = s
        return best

    def func_at(self, line):
        best = None
        for f in self.funcs:
            if f[0] <= line <= f[1]:
                if best is None or (f[1] - f[0]) < (best[1] - best[0]):
                    best = f
        return best

    def class_at(self, line):
        best = None
        for c in self.classes:
            if c[0] <= line <= c[1]:
                if best is None or (c[1] - c[0]) < (best[1] - best[0]):
                    best = c
        return best

    def in_string(self, line):
        return any(a < line <= b for a, b in self.strspans)

    def trivial(self, line):
        """True when the old line is blank or only a comment."""
        if line < 1 or line > len(self.lines):
            return True
        s = self.lines[line - 1].strip()
        if s and not s.startswith("#"):
            return False
        if self.in_string(line):
            return False
        if line <= 2 and CODING.match(self.lines[line - 1]):
            return False
        return True

    @staticmethod
    def header(node):
        """Line span of a statement's own code (excluding nested statements)."""
        start = Src._start(node)
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
            first = Src._start(body[0])
            if first > start:
                return start, first - 1
        return start, node.end_lineno

    def lambda_cols(self, line, c0, c1):
        """Is the column range [c0, c1) of this line inside a lambda body?"""
        for l0, col0, l1, col1 in self.lambdas:
            if l0 == l1 == line and col0 <= c0 and c1 <= col1:
                return True
            if l0 < line < l1:
                return True
        return False


def bound_names(node):
    names = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
        return names
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            names.add((a.asname or a.name).split(".")[0])
        return names
    targets = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
    if not names:
        for sub in ast.walk(node):
            if sub is node:
                continue
            if isinstance(sub, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Import, ast.ImportFrom,
                                ast.FunctionDef, ast.ClassDef)):
                names |= bound_names(sub)
    return names


def module_name(rel):
    m = rel[:-3].replace("/", ".")
    if m.endswith(".__init__"):
        m = m[: -len(".__init__")]
    return m


class Result:
    def __init__(self):
        self.cands = set()
        self.static_all = False  # cannot be checked by running: emit everything
        self.changed = {}  # rel path -> new text (None = deleted)
        self.notes = []
        self.broken = False  # a module no longer parses
        self.focus = []  # (file, first line, last line) of functions whose body changed
        self.cold = False  # something other than function bodies changed: needs a fresh import
        self.hot_lines = {}  # file -> changed line numbers (when line numbering is unchanged)
        self.focused = set()  # candidates that came from executing those functions


class Analyzer:
    def __init__(self, mapdir, src_root, tests):
        self.map = Map(mapdir, tests)
        self.root = src_root
        self._src = {}
        self._text = {}
        self._pyfiles = None
        self._subclasses = None
        self._modusers = {}

    # -- source access (always the pinned, unpatched tree) --
    def read(self, rel):
        t = self._text.get(rel, False)
        if t is False:
            try:
                with open(os.path.join(self.root, rel), encoding="utf-8", newline="") as f:
                    t = f.read()
            except (OSError, UnicodeDecodeError):
                t = None
            self._text[rel] = t
        return t

    def src(self, rel):
        s = self._src.get(rel, False)
        if s is False:
            t = self.read(rel)
            try:
                s = Src(t) if t is not None else None
            except (SyntaxError, ValueError, RecursionError):
                s = None
            self._src[rel] = s
        return s

    def pyfiles(self):
        if self._pyfiles is None:
            out = []
            for top in ("sqlglot", "tests"):
                for dp, dn, fn in os.walk(os.path.join(self.root, top)):
                    dn[:] = [d for d in dn if d != "__pycache__"]
                    for f in fn:
                        if f.endswith(".py"):
                            out.append(os.path.relpath(os.path.join(dp, f), self.root).replace(os.sep, "/"))
            self._pyfiles = sorted(out)
        return self._pyfiles

    # -- main entry --
    def analyze(self, patch_text):
        res = Result()
        try:
            fps = patchlib.parse(patch_text)
        except Exception as e:  # noqa: BLE001
            res.static_all = True
            res.notes.append(f"unparsable patch: {e!r}")
            return res
        if not fps and patch_text.strip():
            res.static_all = True
            res.notes.append("no file sections found")
            return res
        for fp in fps:
            try:
                self._file(fp, res)
            except Exception as e:  # noqa: BLE001
                res.static_all = True
                res.notes.append(f"analysis error on {fp.new or fp.old}: {e!r}")
        if res.cands and self.map.unknown:
            res.cands |= self.map.unknown
        return res

    def _file(self, fp, res):
        m = self.map
        old_rel, new_rel = fp.old, fp.new
        if fp.binary:
            for rel in {old_rel, new_rel} - {None}:
                self._nonpy(rel, res, binary=True)
            return
        paths = {p for p in (old_rel, new_rel) if p}
        for rel in paths:
            base = rel.rsplit("/", 1)[-1]
            if base in ROOT_CONFIG and (rel.count("/") == 0 or base == "conftest.py"):
                # Test-runner configuration: cannot be verified by running tests here.
                # Lines that are blank or comments in these formats change nothing.
                touched = [t for _, _, _, _, body in fp.hunks for tag, t, _ in body if tag != " "]
                if base == "conftest.py" or fp.is_new or fp.new is None or any(
                    t.strip() and not t.strip().startswith(("#", ";")) for t in touched
                ):
                    res.static_all = True
                    res.notes.append(f"config file {rel}")
        old_text = self.read(old_rel) if old_rel else None
        if old_rel and old_text is None and not fp.is_new:
            # patch touches a file that does not exist at the pinned commit
            res.static_all = True
            res.notes.append(f"missing base file {old_rel}")
            return
        if fp.is_new and old_rel is None:
            old_text = None
        try:
            new_text = patchlib.apply(old_text, fp)
        except patchlib.PatchError as e:
            res.static_all = True
            res.notes.append(f"cannot apply {old_rel}: {e}")
            return
        if new_rel is None or (old_rel and new_rel and old_rel != new_rel):
            if old_rel:
                res.changed[old_rel] = None
                self._removed_file(old_rel, res)
        if new_rel is not None:
            res.changed[new_rel] = new_text
            if old_rel != new_rel or old_text is None:
                self._new_file(new_rel, new_text, res)
                return
        if new_rel is None:
            return
        rel = new_rel
        if old_text == new_text:
            return
        opened = m.openers(rel)
        res.cands |= opened
        if opened or not rel.startswith("sqlglot/") or any(h[1] != h[3] for h in fp.hunks):
            res.cold = True
        if rel.endswith(".py") and rel.startswith(("sqlglot/", "tests/")):
            self._py(rel, fp, old_text, new_text, res)
        else:
            self._nonpy(rel, res)

    # -- non-Python files (fixtures, data) --
    def _nonpy(self, rel, res, binary=False):
        m = self.map
        res.cold = True
        got = m.openers(rel)
        res.cands |= got
        if got:
            return
        if not rel.startswith(("tests/", "sqlglot/")):
            res.notes.append(f"unreferenced file {rel}")
            return
        # Nobody opened it from Python; a test may hand the path to duckdb, or
        # build it at run time. Fall back to test files that mention it.
        base = rel.rsplit("/", 1)[-1]
        stem = base.split(".")[0]
        parent = rel.rsplit("/", 2)[-2] if rel.count("/") >= 1 else ""
        hit = set()
        for tf in sorted(m.by_file):
            t = self.read(tf) or ""
            if base in t or (len(stem) > 3 and stem in t) or (parent not in ("", "tests", "fixtures", "sqlglot") and parent in t):
                hit |= m.by_file[tf]
                hit |= m.importer_tests(tf)
        helper = self.read("tests/helpers.py") or ""
        if base in helper or (len(stem) > 3 and stem in helper) or (parent not in ("", "tests", "fixtures") and parent in helper):
            hit |= m.importer_tests("tests/helpers.py")
        res.cands |= hit
        res.cands |= m.openers("dir:" + rel.rsplit("/", 1)[0]) if "/" in rel else set()
        res.notes.append(f"unopened file {rel}: {len(hit)} tests by mention")

    def _removed_file(self, rel, res):
        m = self.map
        res.cold = True
        res.cands |= m.openers(rel)
        if rel.endswith(".py"):
            res.broken = True
            res.cands |= m.importer_tests(rel) | m.file_tests(rel) | m.by_file.get(rel, set())
            res.cands |= m.openers("dir:" + rel.rsplit("/", 1)[0])
        else:
            self._nonpy(rel, res)

    def _new_file(self, rel, text, res):
        m = self.map
        res.cold = True
        parent = rel.rsplit("/", 1)[0] if "/" in rel else "."
        res.cands |= m.openers("dir:" + parent)
        if self.read(rel) is not None:
            # replaces an existing file wholesale
            res.cands |= m.openers(rel) | m.importer_tests(rel) | m.file_tests(rel) | m.by_file.get(rel, set())
            return
        if rel.endswith(".py") and rel.startswith(("sqlglot/", "tests/")):
            base = rel.rsplit("/", 1)[-1][:-3]
            sibling_pkg = os.path.isdir(os.path.join(self.root, parent, base))
            if base in ("__init__", "conftest", "sitecustomize") or sibling_pkg:
                res.static_all = True
                res.notes.append(f"new module {rel} may shadow or hook imports")
        elif not rel.startswith(("sqlglot/", "tests/")):
            base = rel.rsplit("/", 1)[-1]
            if base.endswith(".py") and "/" not in rel:
                res.static_all = True  # a new top-level module can shadow imports
                res.notes.append(f"new top-level module {rel}")

    # -- Python source --
    def _py(self, rel, fp, old_text, new_text, res):
        m = self.map
        try:
            new_tree = ast.parse(new_text)
        except (SyntaxError, ValueError, RecursionError):
            res.broken = True
            res.cold = True
            res.cands |= m.importer_tests(rel) | m.file_tests(rel) | m.by_file.get(rel, set())
            res.notes.append(f"{rel} no longer parses")
            return
        s = self.src(rel)
        if s is None:
            res.cold = True
            res.cands |= m.importer_tests(rel) | m.file_tests(rel) | m.by_file.get(rel, set())
            return
        if ast.dump(s.tree) == ast.dump(new_tree):
            first_two = "\n".join(new_text.split("\n")[:2])
            if not any(CODING.match(l) for l in first_two.split("\n")) or old_text.split("\n")[:2] == new_text.split("\n")[:2]:
                res.notes.append(f"{rel}: same syntax tree")
                return
        removed, inserts = patchlib.changed_old_lines(fp)
        removed_set = set(removed)
        res.hot_lines[rel] = sorted(removed_set)
        new_lines_for = {}
        for after, texts in inserts.items():
            new_lines_for[after] = texts
        done_stmt = set()
        for line in removed:
            if s.trivial(line):
                continue
            repl = None
            texts = inserts.get(line)
            if texts and len(texts) == 1 and (line - 1) not in removed_set and (line + 1) not in removed_set:
                repl = texts[0]
            self._old_line(rel, s, line, repl, res, done_stmt)
        for after, texts in inserts.items():
            if after in removed_set or (after + 1) in removed_set:
                # a rewrite of existing lines: handled above, unless it also adds real code lines
                extra = [t for t in texts if t.strip() and not t.strip().startswith("#")]
                n_removed_here = sum(1 for d in (0, 1) if (after + d) in removed_set)
                if len(extra) <= max(1, n_removed_here):
                    continue
            self._insert(rel, s, after, texts, res)

    def _cov(self, rel, lo, hi, res, focus=None):
        m = self.map
        got = m.line_tests(rel, lo, hi)
        res.cands |= got
        if focus is not None:
            res.focused |= got
            span = (rel, focus[0], focus[1])
            if span not in res.focus:
                res.focus.append(span)
        # Lines that also run while some module is being imported change that
        # module's import-time state, so everything using the module is exposed.
        il = m.implines.get(rel)
        if il:
            mods = set()
            for ln in range(lo, hi + 1):
                v = il.get(str(ln))
                if v:
                    mods.update(v)
            if mods:
                res.cold = True
            for mod in mods:
                res.cands |= self.module_users(mod)

    def signatures(self, res, tests):
        """For each test, the lines it executes inside the changed functions."""
        sig = {n: set() for n in tests}
        for rel, lo, hi in res.focus:
            d = self.map._file(rel)
            lines = d["lines"]
            for ln in range(lo, hi + 1):
                j = lines.get(str(ln))
                if j is None:
                    continue
                key = (rel, ln)
                for n in self.map.decode(d["sets"][j]):
                    s_ = sig.get(n)
                    if s_ is not None:
                        s_.add(key)
        return {n: frozenset(v) for n, v in sig.items()}

    def module_users(self, mod):
        r = self._modusers.get(mod)
        if r is None:
            m = self.map
            r = m.importer_tests(mod) | m.file_tests(mod) | m.by_file.get(mod, set())
            prefix = module_name(mod) + ":"
            for key, bits in m.classes.items():
                if key.startswith(prefix):
                    r |= m.decode(bits)
            self._modusers[mod] = r
        return r

    def _old_line(self, rel, s, line, repl, res, done):
        st = s.innermost(line)
        if st is None:
            # not inside any statement: treat like an insertion point
            res.cold = True
            self._insert(rel, s, line - 1, ["?"], res)
            return
        start, end, node, func, qual = st
        if func is not None:
            lo, hi = s.header(node)
            if not (lo <= line <= hi):
                lo, hi = start, end
            f = s.func_at(line)
            self._cov(rel, lo, hi, res, focus=(f[0], f[1]) if f else None)
            return
        # module or class level
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            res.cold = True
            self._cov(rel, start, end, res)
            if line < node.lineno:  # a decorator
                self._import_time(rel, s, node, qual, res, done)
            return
        if repl is not None:
            old = s.lines[line - 1]
            p = 0
            n = min(len(old), len(repl))
            while p < n and old[p] == repl[p]:
                p += 1
            q = 0
            while q < n - p and old[len(old) - 1 - q] == repl[len(repl) - 1 - q]:
                q += 1
            c0, c1 = p, max(p, len(old) - q)
            b0 = len(old[:c0].encode("utf-8"))
            b1 = len(old[:c1].encode("utf-8"))
            if s.lambda_cols(line, b0, b1):
                lo, hi = s.header(node)
                self._cov(rel, lo, hi, res)
                return
        res.cold = True
        self._import_time(rel, s, node, qual, res, done)

    def _import_time(self, rel, s, node, qual, res, done, depth=0):
        """A statement executed while its module is imported has changed."""
        key = (rel, node.lineno, node.end_lineno)
        if key in done:
            return
        done.add(key)
        m = self.map
        if isinstance(node, ast.ClassDef):
            cq = node.name if not qual else f"{qual}.{node.name}"
            self._class_change(rel, s, cq, node, {node.name}, res, done, depth)
            return
        names = bound_names(node)
        if qual:
            cls = None
            for c in s.classes:
                if c[3] == qual:
                    cls = c[2]
            plain = isinstance(node, (ast.Assign, ast.AnnAssign)) and all(
                isinstance(x, ast.Name) for x in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
            self._class_change(rel, s, qual, cls, names, res, done, depth, stmt_is_plain=plain)
            return
        if rel.startswith("tests/"):
            res.cands |= m.by_file.get(rel, set()) | m.importer_tests(rel)
            return
        if not names:
            res.cands |= m.importer_tests(rel) | m.file_tests(rel)
            res.notes.append(f"{rel}:{node.lineno} import-time statement without names")
            return
        for name in names:
            self._name_refs(rel, name, res, done, depth)

    def _attr_readers(self, names):
        """Tests that execute code reading any of these class attributes, or None
        when some read happens at import time (then every user of the class is exposed)."""
        if not names:
            return None
        m = self.map
        out = set()
        for name in names:
            if len(name) < 3:
                return None
            pat = re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])")
            for f in self.pyfiles():
                t = self.read(f)
                if not t or name not in t:
                    continue
                hits = [i + 1 for i, l in enumerate(t.split("\n")) if name in l and pat.search(l)]
                if not hits:
                    continue
                s2 = self.src(f)
                if s2 is None:
                    return None
                il = m.implines.get(f, {})
                for line in hits:
                    st = s2.innermost(line)
                    if st is None:
                        if s2.trivial(line):
                            continue
                        return None
                    start, end, node, func, qual = st
                    if func is None:
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            return None  # decorator or default argument: evaluated at import
                        if isinstance(node, (ast.Assign, ast.AnnAssign)):
                            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                            value = node.value
                            plain = all(isinstance(x, ast.Name) for x in targets)
                            reads = value is not None and any(
                                (isinstance(n, ast.Name) and n.id == name)
                                or (isinstance(n, ast.Attribute) and n.attr == name)
                                or (isinstance(n, ast.Constant) and isinstance(n.value, str) and name in n.value)
                                for n in ast.walk(value)
                            )
                            if plain and not reads and qual:
                                continue  # another class (re)defining the attribute
                        return None
                    lo, hi = s2.header(node)
                    if not (lo <= line <= hi):
                        lo, hi = start, end
                    if any(str(ln) in il for ln in range(lo, hi + 1)):
                        return None  # read while a module is being imported
                    out |= m.line_tests(f, lo, hi)
        return out

    def _class_change(self, rel, s, qual, cls_node, names, res, done, depth, stmt_is_plain=False):
        m = self.map
        if rel.startswith("tests/"):
            for c in self._with_subclasses(qual.split(".")[0]):
                for (f, cn), nums in m.by_class.items():
                    if cn == c:
                        res.cands |= nums
            return
        key = f"{module_name(rel)}:{qual}"
        users = m.class_users(key)
        at_import = m.classimports.get(key)
        if at_import:
            # the class is instantiated while these modules import: their users are exposed too
            extra = set()
            for mod in at_import:
                extra |= self.module_users(mod)
            res.cands |= extra
            users = users or set()
        if users is not None:
            readers = self._attr_readers(names) if stmt_is_plain else None
            if readers is None:
                res.cands |= users
            else:
                res.cands |= users & readers
                res.notes.append(f"{qual}.{'/'.join(sorted(names))}: {len(users)} users, {len(users & readers)} also read it")
            return
        if cls_node is not None:
            self._cov(rel, Src._start(cls_node), cls_node.end_lineno, res)
        for name in names | {qual.split(".")[-1]}:
            self._name_refs(rel, name, res, done, depth)

    def _with_subclasses(self, name):
        if self._subclasses is None:
            sub = {}
            pat = re.compile(r"^class\s+(\w+)\s*\(([^)]*)\)", re.M)
            for f in self.pyfiles():
                if not f.startswith("tests/"):
                    continue
                t = self.read(f) or ""
                for mm in pat.finditer(t):
                    for base in re.findall(r"[\w.]+", mm.group(2)):
                        sub.setdefault(base.split(".")[-1], set()).add(mm.group(1))
            self._subclasses = sub
        out = {name}
        work = [name]
        while work:
            n = work.pop()
            for c in self._subclasses.get(n, ()):
                if c not in out:
                    out.add(c)
                    work.append(c)
        return out

    def _name_refs(self, rel, name, res, done, depth):
        """Tests that execute code mentioning `name` (a module/class level binding)."""
        m = self.map
        if (name, "name") in done:
            return
        done.add((name, "name"))
        if depth > 3 or len(name) < 2:
            res.cands |= m.importer_tests(rel) | m.file_tests(rel)
            res.notes.append(f"wide name {name}")
            return
        pat = re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])")
        for f in self.pyfiles():
            t = self.read(f)
            if not t or name not in t:
                continue
            hits = [i + 1 for i, l in enumerate(t.split("\n")) if name in l and pat.search(l)]
            if not hits:
                continue
            s2 = self.src(f)
            if s2 is None:
                res.cands |= m.file_tests(f)
                continue
            seen = set()
            for line in hits:
                st = s2.innermost(line)
                if st is None:
                    continue
                start, end, node, func, qual = st
                k = (start, end)
                if k in seen:
                    continue
                seen.add(k)
                if func is not None:
                    lo, hi = s2.header(node)
                    if not (lo <= line <= hi):
                        lo, hi = start, end
                    self._cov(f, lo, hi, res)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._cov(f, start, end, res)
                elif f == rel and (start, end) == (node.lineno, node.end_lineno) and name in bound_names(node) and not qual:
                    # the defining statement itself (or a rebinding): lambdas on it still run later
                    self._cov(f, start, end, res)
                else:
                    self._cov(f, start, end, res)  # lambdas / comprehensions run later
                    self._import_time(f, s2, node, qual, res, done, depth + 1)

    def _insert(self, rel, s, after, texts, res):
        """New lines added after old line `after`."""
        m = self.map
        real = [t for t in texts if t.strip() and not t.strip().startswith("#")]
        cookie = after < 2 and any(CODING.match(t) for t in texts)
        if not real and not cookie and not s.in_string(after + 1):
            return
        res.cold = True
        fa = s.func_at(after) if after >= 1 else None
        fb = s.func_at(after + 1)
        f = fa or fb
        if f is not None:
            same = fa is not None and fb is not None and fa[2] is fb[2]
            self._cov(rel, f[0], f[1], res, focus=(f[0], f[1]) if same else None)
            if same:
                return
        # module or class level (or the edge of a function)
        c = s.class_at(after + 1) or (s.class_at(after) if after >= 1 else None)
        names = set()
        try:
            tree = ast.parse(textwrap.dedent("\n".join(texts)))
            for node in tree.body:
                names |= bound_names(node)
        except (SyntaxError, ValueError):
            names = set()
        done = set()
        if c is not None and c[3]:
            self._class_change(rel, s, c[3], c[2], names, res, done, 0)
            return
        if rel.startswith("tests/"):
            res.cands |= m.by_file.get(rel, set()) | m.importer_tests(rel)
            return
        if not names:
            res.cands |= m.importer_tests(rel) | m.file_tests(rel)
            res.notes.append(f"{rel}: import-time insertion without names")
            return
        for name in names:
            self._name_refs(rel, name, res, done, 0)
