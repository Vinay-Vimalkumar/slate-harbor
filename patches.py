"""Apply unified hunks against the immutable source, checking every context line."""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re


class UnsupportedPatch(ValueError):
    pass


@dataclass
class Change:
    path: str
    before: bytes | None
    after: bytes | None
    old_lines: set[int]
    anchors: set[int]
    mode_change: bool = False


HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
NO_NEWLINE = b"\\ No newline at end of file"


def name(raw, prefix):
    raw = raw.rstrip(b"\r\n").split(b"\t", 1)[0]
    if raw == b"/dev/null":
        return None
    if raw.startswith(prefix):
        raw = raw[len(prefix):]
    value = raw.decode("utf-8")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or str(path) != value or '"' in value:
        raise UnsupportedPatch("unsupported pathname")
    return value


def parse_patch(data: bytes, base: Path) -> list[Change]:
    # Only LF is a record separator in unified diffs; CR may itself be source data.
    rows = [line + b"\n" for line in data.split(b"\n")]
    if rows[-1] == b"\n":
        rows.pop()
    elif rows:
        rows[-1] = rows[-1][:-1]
    result = []
    paths = set()
    i = 0
    pending_header = False
    while i < len(rows):
        row = rows[i]
        if row.startswith(b"diff --git "):
            if pending_header:
                raise UnsupportedPatch("change without text hunks")
            modes = {b"100644", b"100755"}
            if (i + 2 < len(rows) and rows[i+1].startswith(b"old mode ")
                    and rows[i+2].startswith(b"new mode ") and
                    {rows[i+1][9:].strip(), rows[i+2][9:].strip()} == modes):
                header = re.fullmatch(rb"diff --git a/(\S+) b/(\S+)\r?\n?", row)
                if not header or header[1] != header[2] or (i+3 < len(rows) and not rows[i+3].startswith(b"diff --git ")):
                    raise UnsupportedPatch("mixed mode or content edit")
                location = name(header[1], b"")
                if not location or location in paths:
                    raise UnsupportedPatch("invalid mode edit")
                source = base / location
                if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(base.resolve()):
                    raise UnsupportedPatch("unsupported mode target")
                content = source.read_bytes()
                result.append(Change(location, content, content, set(), set(), mode_change=True))
                paths.add(location)
                i += 3
                continue
            if (rows[i+1:i+2] == [b"similarity index 100%\n"] and i+3 < len(rows)
                    and rows[i+2].startswith(b"rename from ") and rows[i+3].startswith(b"rename to ")):
                old = name(rows[i+2][12:], b"")
                new = name(rows[i+3][10:], b"")
                if not old or not new or old == new or paths.intersection((old, new)):
                    raise UnsupportedPatch("invalid rename")
                header = f"diff --git a/{old} b/{new}".encode()
                source, target = base / old, base / new
                if (row.rstrip(b"\r\n") != header or target.exists() or not source.is_file()
                        or source.is_symlink() or not source.resolve().is_relative_to(base.resolve())
                        or not target.resolve().is_relative_to(base.resolve())):
                    raise UnsupportedPatch("unsupported rename")
                content = source.read_bytes()
                result.extend((Change(old, content, None, set(), set()), Change(new, None, content, set(), set())))
                paths.update((old, new))
                i += 4
                continue
            pending_header = True
            i += 1
            continue
        if not row.startswith(b"--- "):
            if row.startswith((b"index ", b"new file mode ", b"deleted file mode ")) or not row.strip():
                i += 1
                continue
            raise UnsupportedPatch("unsupported diff metadata")
        if i + 1 >= len(rows) or not rows[i + 1].startswith(b"+++ "):
            raise UnsupportedPatch("missing target header")
        old = name(row[4:], b"a/")
        new = name(rows[i+1][4:], b"b/")
        path = old or new
        if path is None or path in paths or (old and new and old != new):
            raise UnsupportedPatch("ambiguous file identity")
        src = base / path
        if not src.resolve().is_relative_to(base.resolve()) or (old is None and src.exists()):
            raise UnsupportedPatch("source lookup failed")
        before = src.read_bytes() if old is not None else None
        source = (before or b"").split(b"\n")
        original = [line + b"\n" for line in source[:-1]]
        if source[-1]:
            original.append(source[-1])
        output, removed, anchors = [], set(), set()
        pos = 0
        i += 2
        hunks = 0
        while i < len(rows) and (match := HUNK.match(rows[i])):
            old_start, old_count, new_start, new_count = match.groups()
            a, b = int(old_count or b"1"), int(new_count or b"1")
            start = int(old_start) - int(a > 0)
            if not pos <= start <= len(original):
                raise UnsupportedPatch("hunk position outside file")
            output.extend(original[pos:start])
            if len(output) != int(new_start) - int(b > 0):
                raise UnsupportedPatch("inconsistent new hunk position")
            pos, i = start, i + 1
            hunks += 1
            # Only a pure insertion lacks an old executable location. For a
            # replacement, use its deleted lines instead of the following scope.
            deleted_in_run = False
            while a or b:
                if i >= len(rows) or rows[i][:1] not in (b" ", b"+", b"-"):
                    raise UnsupportedPatch("incomplete hunk")
                marker, body = rows[i][:1], rows[i][1:]
                i += 1
                if i < len(rows) and rows[i].startswith(NO_NEWLINE):
                    body = body.removesuffix(b"\n")
                    i += 1
                if marker in (b" ", b"-"):
                    if a <= 0 or pos >= len(original) or original[pos] != body:
                        raise UnsupportedPatch("source context mismatch")
                    if marker == b"-":
                        removed.add(pos + 1)
                        deleted_in_run = True
                    else:
                        deleted_in_run = False
                    pos += 1
                    a -= 1
                if marker in (b" ", b"+"):
                    if b <= 0:
                        raise UnsupportedPatch("excess destination lines")
                    if marker == b"+" and not deleted_in_run:
                        anchors.update(n for n in (pos, pos + 1) if 1 <= n <= len(original))
                    output.append(body)
                    b -= 1
        if not hunks:
            raise UnsupportedPatch("no text hunks")
        output.extend(original[pos:])
        content = b"".join(output)
        if new is None and content:
            raise UnsupportedPatch("nonempty deletion")
        result.append(Change(path, before, content if new else None, removed, anchors))
        paths.add(path)
        pending_header = False
    if not result or pending_header:
        raise UnsupportedPatch("incomplete diff")
    return result
