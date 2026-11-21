"""Prepare compact, version-specific representations of the immutable base at image build time."""
import gzip
import hashlib
import json
import marshal
from pathlib import Path
import sys


def prepare(app, base):
    from impact import executable, structure, syntax
    from source_reads import _headers

    target = app / "prepared"
    target.mkdir(exist_ok=True)
    with gzip.open(app / "coverage-index.json.gz", "rt") as stream:
        coverage = json.load(stream)
    interned = {}

    def shared(numbers):
        key = tuple(numbers)
        return interned.setdefault(key, key)

    for locations in coverage["lines"].values():
        for line, numbers in locations.items():
            locations[line] = shared(numbers)
    for table in ("files", "reads"):
        coverage[table] = {k: shared(v) for k, v in coverage[table].items()}
    with (target / "coverage.bin").open("wb") as out:
        marshal.dump(coverage, out)

    for root in ("sqlglot", "tests"):
        for file in (base / root).rglob("*.py"):
            source = file.read_bytes()
            path = file.relative_to(base).as_posix()
            try:
                original = compile(source, path, "exec", dont_inherit=True)
                record = (hashlib.sha256(source).digest(), executable(original),
                          hashlib.sha256(marshal.dumps(original)).digest(), syntax(source),
                          structure(source), _headers(source))
            except (SyntaxError, ValueError, RecursionError):
                continue
            dest = target / (path + ".bin")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as out:
                marshal.dump(record, out)


if __name__ == "__main__":
    prepare(Path(__file__).resolve().parent, Path(sys.argv[1]).resolve())
