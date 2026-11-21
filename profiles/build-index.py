"""Observe the pinned suite or combine its per-test observations into a selector index."""
import argparse
from collections import defaultdict
from concurrent import futures
from functools import lru_cache
import gzip
import hashlib
import json
from multiprocessing.reduction import ForkingPickler
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import pytest

LAB = Path(__file__).resolve().parent
SOURCE = LAB / "source"
REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
MANIFEST = LAB / "starter/sample/tests.json"


def round_trip(value):
    return ForkingPickler.loads(ForkingPickler.dumps(value))


class InlinePool:
    """Run the two upstream pools synchronously, including their serialization steps."""
    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        result = futures.Future()
        try:
            fn, args, kwargs = round_trip((fn, args, kwargs))
            result.set_result(round_trip(fn(*args, **kwargs)))
        except Exception as error:
            result.set_exception(error)
        return result

    def shutdown(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.shutdown()


class Observe:
    def __init__(self, catalog, records, todo):
        self.ids = {row["id"]: row["number"] for row in catalog}
        self.records = records
        self.todo = todo
        self.active = False
        self.completed = 0
        self.monitor = sys.monitoring
        self.tool = self.monitor.COVERAGE_ID

    @lru_cache(maxsize=8192)
    def relative(self, filename):
        prefix = str(SOURCE) + os.sep
        return filename[len(prefix):] if filename.startswith(prefix) else None

    def line(self, code, line):
        if self.active:
            path = self.relative(code.co_filename)
            if path is not None:
                self.lines[path].add(line)
        # Each instruction needs one observation per test. restart_events resets this at the next test.
        return self.monitor.DISABLE

    def read(self, event, args):
        if self.active and event == "subprocess.Popen":
            self.subprocess = True
        if self.active and event == "open":
            file, mode, flags = args
            if isinstance(file, (str, bytes)) and not (flags & os.O_ACCMODE == os.O_WRONLY):
                path = self.relative(os.path.abspath(os.fsdecode(file)))
                if path and "__pycache__/" not in path:
                    caller = sys._getframe(1).f_code
                    loader = caller.co_filename.startswith("<frozen importlib.") and caller.co_name == "get_data"
                    if loader:
                        self.import_reads.add(path)
                    else:
                        self.reads.add(path)

    def pytest_collection_modifyitems(self, session, config, items):
        collected = {item.nodeid for item in items}
        missing = set(self.ids) - collected
        if missing:
            raise RuntimeError(f"{len(missing)} catalog tests missing at collection: {min(missing)}")
        items[:] = [item for item in items if self.ids.get(item.nodeid) in self.todo]

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.valid = False

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        number = self.ids[item.nodeid]
        self.lines = defaultdict(set)
        self.reads = set()
        self.import_reads = set()
        self.valid = True
        self.subprocess = False
        self.active = True
        self.monitor.restart_events()
        try:
            return (yield)
        finally:
            self.active = False
            row = {"id": item.nodeid, "valid": self.valid, "subprocess": self.subprocess, "reads": sorted(self.reads),
                   "import_reads": sorted(self.import_reads),
                   "lines": {file: sorted(lines) for file, lines in self.lines.items()}}
            dest = self.records / f"{number}.json"
            tmp = dest.with_suffix(".tmp")
            tmp.write_text(json.dumps(row, separators=(",", ":")))
            tmp.replace(dest)
            self.completed += 1
            if self.completed % 100 == 0:
                print(f"Observed {self.completed}/{len(self.todo)}", flush=True)

    def run(self):
        sys.addaudithook(self.read)
        m = self.monitor
        m.use_tool_id(self.tool, "test-index")
        m.register_callback(self.tool, m.events.LINE, self.line)
        m.set_events(self.tool, m.events.LINE)
        try:
            with patch.object(futures, "ProcessPoolExecutor", InlinePool):
                code = pytest.main(["-p", "no:cacheprovider", "-p", "no:terminal"], plugins=[self])
        finally:
            m.set_events(self.tool, 0)
            m.register_callback(self.tool, m.events.LINE, None)
            m.free_tool_id(self.tool)
        return int(code)


def write_index(catalog, records, dest):
    file_masks = defaultdict(int)
    read_masks = defaultdict(int)
    line_masks = defaultdict(lambda: defaultdict(int))
    fallback = []
    missing = []
    failed = []
    subprocess_tests = []
    for row in catalog:
        n = row["number"]
        mask = 1 << n
        file_masks[row["file"]] |= mask
        record = records / f"{n}.json"
        if not record.is_file():
            fallback.append(n)
            missing.append(n)
            continue
        data = json.loads(record.read_text())
        if data["id"] != row["id"]:
            raise ValueError(f"Wrong catalog identity in observation {n}")
        if not data["valid"]:
            failed.append(n)
        if data.get("subprocess"):
            subprocess_tests.append(n)
        if not data["valid"] or data.get("subprocess"):
            fallback.append(n)
        for path in data.get("import_reads", []):
            if (SOURCE / path).is_file():
                file_masks[path] |= mask
        for path in data["reads"]:
            if not (SOURCE / path).is_file():
                continue
            file_masks[path] |= mask
            read_masks[path] |= mask
        for path, numbers in data["lines"].items():
            file_masks[path] |= mask
            for line in numbers:
                line_masks[path][line] |= mask

    @lru_cache(maxsize=None)
    def ids(mask):
        numbers = []
        while mask:
            low = mask & -mask
            numbers.append(low.bit_length() - 1)
            mask ^= low
        return numbers

    index = {
        "version": 1, "revision": REVISION, "test_count": len(catalog),
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "missing_tests": missing, "failed_or_skipped_tests": failed, "subprocess_tests": subprocess_tests,
        "mode": "per-test setup/call/teardown lines and open reads; inline pickling pool",
        "fallback_tests": fallback,
        "files": {p: ids(mask) for p, mask in sorted(file_masks.items())},
        "reads": {p: ids(mask) for p, mask in sorted(read_masks.items())},
        "lines": {p: {n: ids(mask) for n, mask in sorted(hits.items())}
                  for p, hits in sorted(line_masks.items())},
    }
    # Use deterministic compression and atomically replace any earlier map.
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as out:
        tmp = Path(out.name)
        try:
            with gzip.GzipFile(fileobj=out, mode="wb", filename="", compresslevel=6, mtime=0) as zipped:
                for field, value in index.items():
                    prefix = "{" if field == "version" else ","
                    zipped.write((prefix + json.dumps(field) + ":").encode())
                    # File-at-a-time encoding keeps peak memory independent of uncompressed index size.
                    if field == "lines":
                        zipped.write(b"{")
                        for i, (p, lines) in enumerate(value.items()):
                            if i:
                                zipped.write(b",")
                            zipped.write((json.dumps(p) + ":").encode())
                            zipped.write(json.dumps(lines, separators=(",", ":")).encode())
                        zipped.write(b"}")
                    else:
                        zipped.write(json.dumps(value, separators=(",", ":")).encode())
                zipped.write(b"}")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    if tmp.stat().st_size >= 19_000_000:
        tmp.unlink()
        raise ValueError("Index exceeds the 19 MB local allocation for the 20 MB repository")
    tmp.chmod(0o644)
    tmp.replace(dest)
    print(f"Wrote {dest.stat().st_size:,} bytes, {len(file_masks)} paths, "
          f"{sum(map(len, line_masks.values()))} distinct source lines, {len(fallback)} fallback tests")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["observe", "index"])
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=1620)
    parser.add_argument("--redo", action="store_true", help="re-observe the selected test range")
    parser.add_argument("--ids", type=int, nargs="+", help="restrict observation to the listed test numbers")
    args = parser.parse_args()
    records = args.records.resolve()
    records.mkdir(parents=True, exist_ok=True)
    catalog = json.loads(MANIFEST.read_text())["tests"]
    if args.mode == "index":
        if not args.output:
            parser.error("index requires --output")
        write_index(catalog, records, args.output.resolve())
    else:
        todo = {r["number"] for r in catalog if args.start <= r["number"] < args.end
                and (args.redo or not (records / f"{r['number']}.json").exists())}
        if args.ids:
            todo.intersection_update(args.ids)
        if todo:
            os.chdir(SOURCE)
            sys.path.insert(0, str(SOURCE))
            os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
            raise SystemExit(Observe(catalog, records, todo).run())


if __name__ == "__main__":
    main()
