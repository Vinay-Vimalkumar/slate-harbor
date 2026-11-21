"""Recognize a successful class setup that only stores a fixture's parsed value."""
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path

from fixture_verify import _pinned, _rows
from row_fields import columns


@lru_cache(maxsize=1)
def observations():
    path = Path(__file__).with_name("setup-fixtures.json.gz")
    if not path.is_file():
        return {}
    with gzip.open(path, "rt") as src:
        return json.load(src)["fixtures"]


@lru_cache(maxsize=8)
def source_digest(file):
    return hashlib.sha256(file.read_bytes()).hexdigest()


def unaffected(change, base, candidates):
    if not change.path.startswith("tests/fixtures/") or change.before is None or change.after is None:
        return ()
    entry = observations().get(change.path)
    if not entry:
        return ()
    try:
        if source_digest(base / entry["file"]) != entry["source_hash"]:
            return ()
        pinned = _pinned(base)
        if pinned is None:
            return ()
        # All class members can fail together on a malformed fixture. Evaluate
        # the entire revised loader before relying on recorded attribute usage.
        new = _rows(pinned[0], change.after)
        passed = set(entry["unused"])
        if not entry.get("setup_only"):
            return passed
        old = _rows(pinned[0], change.before)
        if len(new) != len(old):
            return passed
        changed_columns = set()
        for a, b in zip(old, new):
            if tuple(a[0].items()) != tuple(b[0].items()):
                changed_columns.add(0)
            changed_columns.update(i for i in (1, 2) if a[i] != b[i])
        source = (base / entry["file"]).read_bytes()
        for row in candidates:
            n = row["number"]
            if n in passed or n not in entry["setup_only"]:
                continue
            method = row["id"].rsplit("::", 1)[-1]
            used = columns(source, entry["class"], method, entry["field"])
            if not changed_columns.intersection(used):
                passed.add(n)
        return passed
    except (OSError, UnicodeError, ValueError, KeyError, IndexError, StopIteration, SyntaxError):
        return ()
