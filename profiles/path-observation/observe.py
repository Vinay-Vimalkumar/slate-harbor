"""Observe directory enumeration, stat checks and import searches on the pristine tree."""
from collections import defaultdict
import ast
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
import gzip
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


LAB = Path(__file__).resolve().parents[1]
BASE = LAB / "source"
MANIFEST = LAB / "starter/sample/tests.json"
OUT = LAB / ".path-observations"
INDEX = LAB.parent / "solution/path-index.json.gz"


def worker(testfile, dest):
    helpers = importlib.util.spec_from_file_location("probe_pool", LAB / "build-index.py")
    module = importlib.util.module_from_spec(helpers)
    helpers.loader.exec_module(module)
    from unittest.mock import patch
    import pytest
    prefix = str(BASE) + os.sep
    dirs, probes = set(), set()
    children = 0
    def record(files, path):
        if isinstance(path, (str, bytes, os.PathLike)):
            name = os.path.abspath(os.fsdecode(path))
            if name.startswith(prefix) and "/__pycache__" not in name:
                files.add(name[len(prefix):])
    def audit(event, args):
        nonlocal children
        if event in ("os.listdir", "os.scandir"):
            # FileFinder caches directory listings for arbitrary imports. Record
            # the actual module it wanted in find_spec instead.
            if not sys._getframe(1).f_code.co_filename.startswith("<frozen importlib."):
                record(dirs, args[0])
        elif event == "open":
            record(probes, args[0])
        elif event == "subprocess.Popen":
            children += 1
    stat, lstat = os.stat, os.lstat
    def observed(fn):
        def wrapped(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                record(probes, path)
            return fn(path, *args, **kwargs)
        return wrapped
    finder = importlib.machinery.FileFinder
    original = finder.find_spec
    def find(self, name, target=None):
        spec = original(self, name, target)
        if spec is None or spec.loader is None:
            basename = name.rsplit(".", 1)[-1]
            record(probes, os.path.join(self.path, basename + ".py"))
        return spec
    os.chdir(BASE)
    sys.path.insert(0, str(BASE))
    sys.addaudithook(audit)
    passed, failures = set(), set()
    class Reports:
        def pytest_runtest_logreport(self, report):
            if report.failed or report.skipped:
                failures.add(report.nodeid)
            elif report.when == "call" and report.passed:
                passed.add(report.nodeid)
    with tempfile.TemporaryDirectory() as temp:
        os.environ.update(QUERY_CHILD_DIR=temp, BASE_DIR_PREFIX=str(BASE),
                          PYTHONPATH=str(Path(__file__).parent/"child")+os.pathsep+os.environ.get("PYTHONPATH",""))
        with patch.object(os,"stat",observed(stat)), patch.object(os,"lstat",observed(lstat)), \
                patch.object(finder,"find_spec",find), patch.object(concurrent.futures,"ProcessPoolExecutor",module.InlinePool):
            result = pytest.main(["-q","-p","no:cacheprovider","--timeout=600",testfile],plugins=[Reports()])
        reports = list(Path(temp).glob("*.json"))
        if len(reports) == children == len(list(Path(temp).glob("*.start"))):
            for report in reports:
                data = json.loads(report.read_bytes())
                dirs.update(data["dirs"])
                probes.update(data["probes"])
            children = 0
    dest.write_text(json.dumps(dict(status=int(result), dirs=sorted(dirs), probes=sorted(probes),
                                    children=children,passed=sorted(passed-failures))))


def main():
    rows = json.loads(MANIFEST.read_bytes())["tests"]
    files = defaultdict(list)
    for row in rows:
        files[row["file"]].append(row["number"])
    OUT.mkdir(exist_ok=True)
    def launch(file):
        dest = OUT / (file.replace("/","-") + ".json")
        if not dest.is_file():
            env = dict(os.environ, PYTHON_CPU_COUNT="2", SKIP_INTEGRATION="0", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                       PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ["PATH"])
            with tempfile.NamedTemporaryFile(dir=OUT) as tmp:
                payload = Path(tmp.name)
                with open(os.devnull, "wb") as quiet:
                    subprocess.run([sys.executable, __file__,"worker",file,str(payload)], stdout=quiet,stderr=quiet, env=env,check=True)
                dest.write_bytes(payload.read_bytes())
        return json.loads(dest.read_bytes())
    dirs, paths, unknown = defaultdict(set), defaultdict(set), set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        for file, obs in zip(files,pool.map(launch,files)):
            numbers = files[file]
            if obs["children"] or obs["status"] not in (0,1) or obs["status"] and "passed" not in obs:
                unknown.update(numbers)
            elif "passed" in obs:
                unknown.update(n for n in numbers if rows[n]["id"] not in obs["passed"])
            for d in obs["dirs"]:
                dirs[d].update(numbers)
            for p in obs["probes"]:
                paths[p].update(numbers)
    masks = {}
    def ids(group):
        value = tuple(sorted(group))
        return masks.setdefault(value,len(masks))
    result = {"dirs":{k:ids(v) for k,v in sorted(dirs.items())},
              "paths":{k:ids(v) for k,v in sorted(paths.items())}, "unknown":sorted(unknown)}
    result["sets"]=list(masks)
    result["nodes"]=[row["id"] for row in rows]
    result["files"] = sorted({root for root in ("sqlglot", "tests/fixtures")} | {
        f.relative_to(BASE).as_posix() for root in ("sqlglot", "tests/fixtures")
        for f in (BASE/root).rglob("*") if "__pycache__" not in f.parts})
    queries = {"stat", "lstat", "getsize", "getmtime", "getctime", "getatime", "access",
               "is_file", "isfile", "exists", "lexists", "samefile", "which", "readlink"}
    sites, execution = defaultdict(list), defaultdict(list)
    for root in ("sqlglot","tests"):
        for f in (BASE/root).rglob("*.py"):
            tree = ast.parse(f.read_bytes())
            imports = {a.asname or a.name for imp in ast.walk(tree) if isinstance(imp,ast.ImportFrom)
                       and imp.module in {"os","os.path","pathlib","shutil","posixpath"} for a in imp.names
                       if a.name in queries}
            for n in ast.walk(tree):
                if (isinstance(n,ast.Call) and
                        (isinstance(n.func,ast.Attribute) and n.func.attr in queries
                         or isinstance(n.func,ast.Name) and n.func.id in imports)):
                    sites[f.relative_to(BASE).as_posix()].append(n.lineno)
                if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                        and n.value.id in {"os", "subprocess", "shutil"} and
                        (n.attr in {"system", "run", "Popen", "which", "check_output", "check_call", "call"}
                         or n.attr.startswith(("exec", "spawn")))):
                    execution[f.relative_to(BASE).as_posix()].append(n.lineno)
    result["metadata_sites"] = {k:sorted(set(v)) for k,v in sites.items()}
    result["execution_sites"] = {k:sorted(set(v)) for k,v in execution.items()}
    with gzip.GzipFile(filename=str(INDEX),mode="wb",mtime=0) as out:
        out.write(json.dumps(result,separators=(",",":")).encode())
    print(f"{len(files)} independently collected modules; {len(unknown)} uncertain; {len(dirs)} directories; {len(paths)} paths", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] and sys.argv[1] == "worker":
        worker(sys.argv[2],Path(sys.argv[3]))
    else:
        main()
