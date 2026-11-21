"""Record one complete pristine test process at a time and intern its dependency bitsets."""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
import gzip
import hashlib
import importlib.util
import json
import marshal
import os
from pathlib import Path
import subprocess
import sys
import tempfile

LAB = Path(__file__).resolve().parent
BASE = LAB / "source"
CATALOG = LAB / "starter/sample/tests.json"
RECORDS = LAB / ".isolated-observations"
REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def write(path, record):
    path.parent.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        with gzip.GzipFile(filename="", mode="wb", fileobj=tmp, compresslevel=2, mtime=0) as out:
            marshal.dump(record, out, 4)
        temp = Path(tmp.name)
    temp.chmod(0o644)
    temp.replace(path)


def observe(n, nodeid):
    spec = importlib.util.spec_from_file_location("base_observation", LAB / "build-index.py")
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    # The helper imports pytest and the pure stdlib adapter, but never SQLGlot or its tests.
    if any(k.split(".")[0] in {"tests", "sqlglot"} for k in sys.modules):
        raise RuntimeError("source imported before observation")
    from unittest.mock import patch
    import pytest
    os.chdir(BASE)
    sys.path.insert(0, str(BASE))
    prefix = str(BASE) + os.sep
    lines, reads, modules = defaultdict(set), set(), set()
    child_count = 0
    children = tempfile.TemporaryDirectory(prefix="isolate-child-") if os.environ.get("BASELINE_RECORD_CHILD") else None
    if children:
        os.environ["BASELINE_TRACE_SOURCE"] = str(BASE)
        os.environ["BASELINE_TRACE_CHILD"] = children.name
        os.environ["PYTHONPATH"] = str(LAB / "subprocess-trace") + os.pathsep + os.environ.get("PYTHONPATH", "")

    def record(code, number):
        path = code.co_filename
        if path.startswith(prefix):
            relative = path[len(prefix):]
            lines[relative].add(number)
            if code.co_name == "<module>":
                modules.add(relative)
        return sys.monitoring.DISABLE

    def audit(event, args):
        nonlocal child_count
        if event == "subprocess.Popen":
            child_count += 1
        if event != "open":
            return
        filename, mode, flags = args
        if not isinstance(filename, (str, bytes)) or (flags & os.O_ACCMODE) == os.O_WRONLY:
            return
        filename = os.path.abspath(os.fsdecode(filename))
        if not filename.startswith(prefix) or "/__pycache__/" in filename:
            return
        caller = sys._getframe(1).f_code
        if caller.co_filename.startswith("<frozen importlib."):
            return
        reads.add(filename[len(prefix):])

    sys.addaudithook(audit)
    mon = sys.monitoring
    mon.use_tool_id(mon.COVERAGE_ID, "isolated")
    mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, record)
    mon.set_events(mon.COVERAGE_ID, mon.events.LINE)
    with patch.object(concurrent.futures, "ProcessPoolExecutor", helpers.InlinePool):
        status = pytest.main(["-q", "-p", "no:cacheprovider", "--timeout=300", nodeid])
    mon.set_events(mon.COVERAGE_ID, 0)
    untracked = bool(child_count)
    if children:
        observations = list(Path(children.name).glob("*.json"))
        if len(observations) == child_count:
            untracked = False
            for report in observations:
                child = json.loads(report.read_bytes())
                for path, hits in child["lines"].items():
                    lines[path].update(hits)
                reads.update(child["reads"])
                modules.update(child["modules"])
        children.cleanup()
    out = {"node": nodeid, "exit": int(status), "subprocess": untracked,
           "lines": {path: sorted(v) for path, v in lines.items()}, "reads": sorted(reads), "modules": sorted(modules)}
    write(RECORDS / f"{n}.gz", out)


def launch(row):
    n = row["number"]
    if (RECORDS / f"{n}.gz").is_file():
        return
    env = dict(os.environ, SKIP_INTEGRATION="0", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
               PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    run = subprocess.run([sys.executable, __file__, "worker", str(n), row["id"]], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if run.returncode:
        raise RuntimeError(f"failed to record {n} ({run.returncode})")


def aggregate(rows, dest):
    lines, reads, modules, uncertain, missing = defaultdict(dict), defaultdict(int), defaultdict(int), 0, 0
    for row in rows:
        n = row["number"]
        bit = 1 << n
        path = RECORDS / f"{n}.gz"
        if not path.is_file():
            missing |= bit
            continue
        with gzip.open(path, "rb") as inp:
            obs = marshal.load(inp)
        if obs["node"] != row["id"]:
            raise ValueError("catalog mismatch")
        if obs["exit"] or obs["subprocess"]:
            uncertain |= bit
        for file, hits in obs["lines"].items():
            values = lines[file]
            for hit in hits:
                values[hit] = values.get(hit, 0) | bit
        for file in obs["reads"]:
            reads[file] |= bit
        for file in obs["modules"]:
            modules[file] |= bit
    interned = {0: 0}
    def slot(mask):
        return interned.setdefault(mask, len(interned))
    index = {"revision":REVISION, "version":1, "manifest":hashlib.sha256(CATALOG.read_bytes()).hexdigest(),
             "nodes":[row["id"] for row in rows], "missing":missing, "uncertain":uncertain,
             "lines":{p:{k:slot(v) for k,v in hits.items()} for p,hits in lines.items()},
             "reads":{p:slot(v) for p,v in reads.items()}, "modules":{p:slot(v) for p,v in modules.items()}}
    index["masks"] = list(interned)
    write(dest, index)
    print(f"{len(rows)-missing.bit_count()}/{len(rows)} observed, {uncertain.bit_count()} additional fallback tests, "
          f"{len(interned)} distinct masks, {dest.stat().st_size} bytes", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "worker":
        observe(int(sys.argv[2]), sys.argv[3])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("mode", choices=["run","pack"])
        parser.add_argument("--jobs", type=int, default=3)
        parser.add_argument("--output", type=Path, default=LAB.parent / "solution/isolated-index.gz")
        args = parser.parse_args()
        catalog = json.loads(CATALOG.read_bytes())["tests"]
        if args.mode == "run":
            # Start high-cost tests early to avoid leaving one long tail.
            todo = sorted(catalog, key=lambda r:-r["seconds"])
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                for _ in pool.map(launch, todo):
                    pass
        aggregate(catalog, args.output)
