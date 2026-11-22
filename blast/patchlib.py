"""Minimal unified-diff parser and applier (no git or patch binary needed)."""

import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


class FilePatch:
    __slots__ = ("old", "new", "hunks", "binary", "is_new", "is_delete", "rename")

    def __init__(self):
        self.old = None
        self.new = None
        self.hunks = []  # (old_start, old_len, new_start, new_len, [(tag, text, has_newline)])
        self.binary = False
        self.is_new = False
        self.is_delete = False
        self.rename = False


def _strip(p):
    p = p.rstrip("\r")
    p = p.split("\t")[0].strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def parse(text):
    """Returns a list of FilePatch."""
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            m = re.match(r"^diff --git a/(.*?) b/(.*?)\r?$", line)
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
            i += 1
            continue
        if cur is None:
            if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
                cur = FilePatch()
                files.append(cur)
            else:
                i += 1
                continue
        if line.startswith("new file mode"):
            cur.is_new = True
        elif line.startswith("deleted file mode"):
            cur.is_delete = True
        elif line.startswith("rename from "):
            cur.rename = True
            cur.old = line[len("rename from "):].strip()
        elif line.startswith("rename to "):
            cur.rename = True
            cur.new = line[len("rename to "):].strip()
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            cur.binary = True
        elif line.startswith("--- "):
            o = _strip(line[4:])
            if o is None:
                cur.is_new = True
                cur.old = None
            else:
                cur.old = o
        elif line.startswith("+++ "):
            nw = _strip(line[4:])
            if nw is None:
                cur.is_delete = True
                cur.new = None
            else:
                cur.new = nw
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if not m:
                raise PatchError("bad hunk header")
            os_ = int(m.group(1))
            ol = int(m.group(2)) if m.group(2) is not None else 1
            ns = int(m.group(3))
            nl = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            oc = nc = 0
            while i < n and (oc < ol or nc < nl):
                h = lines[i]
                if h.startswith("\\"):
                    if body:
                        body[-1] = (body[-1][0], body[-1][1], False)
                    i += 1
                    continue
                tag = h[:1]
                if tag == "":
                    tag = " "
                if tag == " ":
                    oc += 1
                    nc += 1
                elif tag == "-":
                    oc += 1
                elif tag == "+":
                    nc += 1
                else:
                    raise PatchError("bad hunk line")
                body.append((tag, h[1:], True))
                i += 1
            if i < n and lines[i].startswith("\\"):
                if body:
                    body[-1] = (body[-1][0], body[-1][1], False)
                i += 1
            if oc != ol or nc != nl:
                raise PatchError("short hunk")
            cur.hunks.append((os_, ol, ns, nl, body))
            continue
        i += 1
    return files


def apply(old_text, fp):
    """Apply one FilePatch to old_text (str, or None for a new file).

    Returns the new text, or None if the file is deleted."""
    if fp.binary:
        raise PatchError("binary")
    if fp.new is None:
        return None
    if old_text is None or old_text == "":
        old_lines = []
        end_nl = True
    else:
        end_nl = old_text.endswith("\n")
        old_lines = old_text.split("\n")
        if end_nl:
            old_lines.pop()
    out = []
    pos = 0
    total = len(old_lines)
    for os_, ol, ns, nl, body in fp.hunks:
        start = os_ - 1 if ol > 0 else os_
        if start < pos or start > total:
            raise PatchError("hunk out of range")
        out.extend(old_lines[pos:start])
        pos = start
        for tag, text, nlflag in body:
            if tag == " ":
                if pos >= total or old_lines[pos] != text:
                    raise PatchError("context mismatch")
                out.append(text)
                pos += 1
                if pos == total:
                    end_nl = nlflag
            elif tag == "-":
                if pos >= total or old_lines[pos] != text:
                    raise PatchError("remove mismatch")
                pos += 1
            else:
                out.append(text)
                if pos == total:
                    end_nl = nlflag
    out.extend(old_lines[pos:])
    if not out:
        return ""
    return "\n".join(out) + ("\n" if end_nl else "")


def changed_old_lines(fp):
    """Returns (removed, inserts).

    removed: old line numbers that the patch removes or rewrites.
    inserts: {old_line_after_which_text_is_added: [added lines]} (0 = file start).
    """
    removed = []
    inserts = {}
    for os_, ol, ns, nl, body in fp.hunks:
        cur = os_ if ol > 0 else os_ + 1
        for tag, text, _ in body:
            if tag == " ":
                cur += 1
            elif tag == "-":
                removed.append(cur)
                cur += 1
            else:
                inserts.setdefault(cur - 1, []).append(text)
    return removed, inserts
