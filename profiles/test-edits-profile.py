"""Trace test-source readers in separate test executions forked after pristine per-file collection."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

LAB = Path(__file__).resolve().parent
BASE = LAB / "source"
ROWS = json.loads((LAB / "starter/sample/tests.json").read_bytes())["tests"]
DEST = LAB.parent / "solution/test-edits.json.gz"
RECORDS = LAB / ".test-edits-records"


def worker(file):
    import importlib.util
    from unittest.mock import patch
    import pytest
    spec = importlib.util.spec_from_file_location("pinned_pool", LAB / "build-index.py")
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    source = str(BASE) + os.sep
    wanted = {r["id"]: r["number"] for r in ROWS if r["file"] == file}
    paths = {r["file"] for r in ROWS}
    reads = set()
    rewrites = set()
    external = False
    failed = False
    own_fork = False
    os.chdir(BASE)
    sys.path.insert(0, str(BASE))

    def audit(event, args):
        nonlocal external
        if (event in {"subprocess.Popen", "_posixsubprocess.fork_exec", "os.system", "os.posix_spawn", "os.exec", "os.forkpty"}
                or (event == "os.fork" and not own_fork)):
            external = True
        if event != "open" or not isinstance(args[0], (str, bytes)):
            return
        name = os.path.abspath(os.fsdecode(args[0]))
        if not name.startswith(source) or name[len(source):] not in paths:
            return
        path = name[len(source):]
        frame = sys._getframe(1)
        while frame and frame.f_globals.get("__name__", "").split(".")[0] in {"pathlib", "io", "_io", "codecs"}:
            frame = frame.f_back
        if frame:
            module = frame.f_globals.get("__name__", "")
            function = frame.f_code.co_name
            parent = frame.f_back
            caller = (parent.f_globals.get("__name__", ""), parent.f_code.co_name) if parent else ("", "")
            if (module in {"importlib._bootstrap_external", "_frozen_importlib_external"}
                    and function == "get_data" and caller == (module, "get_code")):
                return
            if (module == "_pytest.assertion.rewrite" and function in {"_rewrite_test", "_read_pyc"}
                    and caller == (module, "exec_module")):
                rewrites.add(path)
                return
        reads.add(path)

    definitions = {}
    observations = {}

    class TrackedPool(helpers.InlinePool):
        def submit(self, *args, **kwargs):
            nonlocal external
            external = True
            return super().submit(*args, **kwargs)

    class Observe:
        def pytest_runtest_logreport(self, report):
            nonlocal failed
            failed |= not report.passed

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtestloop(self, session):
            nonlocal own_fork
            collect_reads = sorted(reads)
            collect_unsupported = external
            for item in session.items:
                if item.nodeid not in wanted:
                    continue
                n = wanted[item.nodeid]
                method = item.obj
                code = getattr(method, "__code__", None)
                if code and code.co_filename.startswith(source):
                    definitions[n] = [code.co_filename[len(source):], code.co_qualname, code.co_firstlineno]
                # No test has run in the parent. Each child starts with the same fresh module state.
                with tempfile.TemporaryFile(mode="w+b") as result:
                    own_fork = True
                    pid = os.fork()
                    own_fork = False
                    if pid == 0:
                        try:
                            signal.alarm(45)
                            reads.clear()
                            item.ihook.pytest_runtest_protocol(item=item, nextitem=None)
                            data = [sorted(reads | set(collect_reads)), sorted(rewrites), external or failed or collect_unsupported]
                            result.write(json.dumps(data).encode())
                            result.flush()
                            os._exit(0)
                        except BaseException:
                            os._exit(1)
                    # Native extension code can postpone Python's alarm handler.
                    deadline = time.monotonic() + 60
                    while True:
                        done, status = os.waitpid(pid, os.WNOHANG)
                        if done:
                            break
                        if time.monotonic() >= deadline:
                            os.kill(pid, signal.SIGKILL)
                            _, status = os.waitpid(pid, 0)
                            break
                        time.sleep(0.01)
                    if status == 0:
                        result.seek(0)
                        observations[n] = json.load(result)
            return True

    sys.addaudithook(audit)
    with patch("concurrent.futures.ProcessPoolExecutor", TrackedPool):
        status = pytest.main(["-p", "no:cacheprovider", "-p", "no:terminal", "--timeout=40", file], plugins=[Observe()])
    if status != 0:
        raise RuntimeError("collection failed for " + file)
    record = RECORDS / (file.replace("/", "--") + ".json")
    record.with_suffix(".tmp").write_text(json.dumps([definitions, observations]))
    record.with_suffix(".tmp").replace(record)


def aggregate():
    defs, readers, rewrites, known = {}, {}, {}, set()
    for p in sorted(RECORDS.glob("*.json")):
        definitions, observations = json.loads(p.read_bytes())
        for n, (f, q, line) in definitions.items():
            defs.setdefault(f, {}).setdefault(f"{q}@{line}", []).append(int(n))
        for n, (raw, standard, uncertain) in observations.items():
            for path in raw:
                readers.setdefault(path, []).append(int(n))
            if uncertain:
                continue
            known.add(int(n))
            for path in standard:
                rewrites.setdefault(path, []).append(int(n))
    # Only remove a cold reader where this very pair had an attributed rewriting read
    # and no intentional open; every other cold reader remains in selection.
    approved = {p: sorted(set(ns) - set(readers.get(p, ()))) for p, ns in rewrites.items()}
    digests = {p:hashlib.sha256((BASE/p).read_bytes()).hexdigest() for p in defs}
    result = {"files": digests, "definitions": defs, "rewrite_only": approved, "readers":readers,
              "catalog": hashlib.sha256("\n".join(r["id"] for r in ROWS).encode()).hexdigest()}
    tmp = DEST.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(json.dumps(result, separators=(",", ":")).encode(), mtime=0))
    tmp.replace(DEST)
    print(f"{len(known)} individual complete, {len(defs)} definition files, {sum(map(len,approved.values()))} attributed rewrite-only pairs, {len(readers)} files with raw readers, {DEST.stat().st_size} bytes")


def launch(file):
    if not (RECORDS / (file.replace("/", "--") + ".json")).exists():
        env = dict(os.environ, SKIP_INTEGRATION="0", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                   PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
        subprocess.run([sys.executable, "-B", __file__, "worker", file], env=env, check=True)


if __name__ == "__main__":
    if sys.argv[1] == "worker":
        record = RECORDS / (sys.argv[2].replace("/", "--") + ".json")
        with record.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not record.exists():
                worker(sys.argv[2])
    elif sys.argv[1] == "pack":
        aggregate()
    else:
        RECORDS.mkdir(exist_ok=True)
        with ThreadPoolExecutor(max_workers=int(sys.argv[2]) if len(sys.argv)>2 else 2) as pool:
            for _ in pool.map(launch, sorted({r["file"] for r in ROWS})):
                pass
        aggregate()
