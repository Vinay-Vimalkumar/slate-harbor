"""Build a baseline comparison index using real isolated tests and their normal children."""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import fcntl
import gzip
import hashlib
import importlib.util
from importlib._bootstrap_external import _code_to_timestamp_pyc
import json
import os
import resource
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time

LAB = Path(__file__).resolve().parent
BASE = LAB / "source"
TREE = LAB / ".relation-tree"
RECORDS = LAB / ".relation-records"
TABLE = RECORDS / "sites.json"
MANIFEST = LAB / "starter/sample/tests.json"
OUT = LAB.parent / "solution/relation-index.json.gz"
BOOT = LAB / "relation-bootstrap"
OPS = {"Is": 0, "IsNot": 1, "Eq": 2, "NotEq": 3}
NATIVE = False
LEGACY_MINIMUM = .3
RETRY_UNCERTAIN = False
REFRESH = False
REQUIRE_WITNESS = False


def set_native():
    global NATIVE, TREE, RECORDS, TABLE, OUT
    NATIVE = True
    OPS.update(Lt=4, LtE=5, Gt=6, GtE=7)
    TREE = LAB / ".relation-tree-native"
    RECORDS = LAB / ".relation-records-native"
    TABLE = RECORDS / "sites.json"
    OUT = LAB.parent / "solution/relation-order-index.json.gz"


def integer_literal(expr):
    sign = 1
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, (ast.USub, ast.UAdd)):
        sign, expr = (-1 if isinstance(expr.op, ast.USub) else 1), expr.operand
    return isinstance(expr, ast.Constant) and type(expr.value) is int and -(2**63) <= sign * expr.value < 2**63



class Observe(ast.NodeTransformer):
    def __init__(self, path, sites):
        self.path = path
        self.sites = sites

    def visit_Compare(self, node):
        flags = 0
        if NATIVE and len(node.ops) == 1 and not isinstance(node.ops[0], (ast.Is, ast.IsNot)):
            left, right = integer_literal(node.left), integer_literal(node.comparators[0])
            if left != right:
                flags = 16 if left else 32
        self.generic_visit(node)
        if len(node.ops) != 1 or (op := type(node.ops[0]).__name__) not in OPS:
            return node
        label = f"{self.path}:{node.lineno}:{node.col_offset}:{op}"
        n = len(self.sites)
        self.sites.append(label)
        replacement = ast.Call(ast.Name("_rwx_operand_check_70a", ast.Load()),
                               [ast.Constant(n), ast.Constant(OPS[op] | flags), node.left, node.comparators[0]], [])
        return ast.copy_location(replacement, node)


def compile_recorder():
    extension = BOOT / ("_relation_native" + sysconfig.get_config_var("EXT_SUFFIX"))
    flags = ["-undefined", "dynamic_lookup"] if sys.platform == "darwin" else []
    subprocess.run(["cc", "-O3", "-Wall", "-fPIC", "-shared", *flags,
                    "-I" + sysconfig.get_path("include"), str(BOOT / "recorder.c"),
                    "-o", str(extension) + ".new"], check=True)
    Path(str(extension) + ".new").replace(extension)


def build():
    if NATIVE:
        compile_recorder()
    if not TREE.exists():
        shutil.copytree(BASE, TREE, ignore=shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache"))
    RECORDS.mkdir(exist_ok=True)
    sites = []
    for path in sorted((TREE / "sqlglot").rglob("*.py")):
        data = path.read_bytes()
        if data != (BASE / path.relative_to(TREE)).read_bytes():
            raise ValueError(f"private source differs from pristine base: {path}")
        tree = Observe(path.relative_to(TREE).as_posix(), sites).visit(ast.parse(data))
        code = compile(ast.fix_missing_locations(tree), str(path), "exec")
        if NATIVE:
            marker = ("\0rwx-rel-observed-v1/" + path.relative_to(TREE).as_posix()).encode()
            code = code.replace(co_consts=code.co_consts + (marker,))
        dest = Path(importlib.util.cache_from_source(str(path)))
        dest.parent.mkdir(exist_ok=True)
        staging = dest.with_suffix(".pending")
        staging.write_bytes(_code_to_timestamp_pyc(code, int(path.stat().st_mtime), len(data)))
        staging.replace(dest)
    staging = TABLE.with_suffix(".pending")
    staging.write_text(json.dumps(sites))
    staging.replace(TABLE)
    hashes = {p.relative_to(TREE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (TREE / "sqlglot").rglob("*.py")}
    hashes_path = RECORDS / "hashes.json"
    staging = hashes_path.with_suffix(".pending")
    staging.write_text(json.dumps(hashes))
    staging.replace(hashes_path)
    print(f"Instrumented {len(sites)} comparison sites", flush=True)


def test(row):
    out = RECORDS / (str(row["number"]) + ".json")
    if out.exists() and not REFRESH:
        prior = json.loads(out.read_bytes())
        current = prior.get("instrumentation") == (3 if NATIVE else 2)
        witnessed = not REQUIRE_WITNESS or "visited" in prior
        if current and witnessed and (prior["valid"] or not RETRY_UNCERTAIN):
            return
    with tempfile.TemporaryDirectory() as root:
        env = dict(os.environ, REL_SOURCE=str(TREE), REL_OUT=root, PYTHONPATH=os.pathsep.join((str(BOOT), str(TREE))),
                   PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="1", OPENBLAS_NUM_THREADS="1",
                   OMP_NUM_THREADS="1", PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
        if NATIVE:
            env["REL_NATIVE"] = "1"
        t = time.monotonic()
        u = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--timeout=300", row["id"]],
                               cwd=TREE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=360)
        except subprocess.TimeoutExpired:
            p = subprocess.CompletedProcess([], 124, b"observation timed out")
        v = resource.getrusage(resource.RUSAGE_CHILDREN)
        child_cpu = v.ru_utime + v.ru_stime - u.ru_utime - u.ru_stime
        child_dir = Path(root)
        for _ in range(120):
            if len(list(child_dir.glob("*.start"))) == len(list(child_dir.glob("*.json"))):
                break
            time.sleep(.05)
        starts = {f.stem for f in child_dir.glob("*.start")}
        records = {f.stem for f in child_dir.glob("*.json")}
        valid = bool(starts) and starts == records and p.returncode == 0 and b"1 passed" in p.stdout
        good, bad, reads = set(), set(), set()
        intervals, invalid, reasons = {}, set(), set()
        if not starts:
            reasons.add("no instrumented process started")
        if starts != records:
            reasons.add("incomplete instrumented process set")
        if p.returncode or b"1 passed" not in p.stdout:
            reasons.add("baseline did not report one passing test")
        verified_modules, unverified_modules = set(), set()
        all_witnessed = True
        for pid in records:
            try:
                part = json.loads((child_dir / (pid + ".json")).read_bytes())
            except (OSError, ValueError):
                valid = all_witnessed = False
                reasons.add("incomplete process receipt")
                continue
            seen, unreliable, read, unknown = part[:4]
            if NATIVE:
                for site, lo, hi in part[4]:
                    if site in intervals:
                        left, right = intervals[site]
                        lo, hi = max(lo, left), min(hi, right)
                    intervals[site] = (lo, hi)
                invalid.update(part[5])
                if len(part) > 6:
                    reasons.update(part[6])
                if len(part) < 9:
                    all_witnessed = False
                else:
                    verified_modules.update(part[7])
                    unverified_modules.update(part[8])
            good.update(seen); bad.update(unreliable); reads.update(read)
            valid &= not unknown
        record = {"instrumentation": 3 if NATIVE else 2, "node": row["id"], "valid": valid, "good": sorted(good - bad), "reads": sorted(reads),
                  "children": len(starts) - 1, "child_cpu": round(child_cpu, 3), "exit": p.returncode, "seconds": round(time.monotonic()-t, 3),
                  "output_tail": p.stdout[-300:].decode(errors="replace")}
        if NATIVE:
            record["reasons"] = sorted(reasons)
            if all_witnessed and valid:
                record["verified_modules"] = sorted(verified_modules - unverified_modules)
                record["visited"] = sorted(good | bad)
            record["ranges"] = [(site, *intervals[site]) for site in sorted(intervals.keys() - invalid)]
        with tempfile.NamedTemporaryFile(dir=RECORDS, prefix=f".{out.stem}-", suffix=".tmp", mode="w", delete=False) as stream:
            json.dump(record, stream)
            temp = Path(stream.name)
        temp.replace(out)
        print(row["number"], "eligible" if valid else "uncertain", len(good - bad), len(starts), p.returncode, flush=True)


def combine(rows):
    with (RECORDS / "index.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        write_snapshot(rows)


def write_snapshot(rows):
    sites = json.loads(TABLE.read_bytes())
    proofs, readers, tracked, unknown, ranges = {}, {}, [], [], {}
    verified, visits = {}, {}
    for row in rows:
        if not NATIVE and row["seconds"] < LEGACY_MINIMUM:
            continue
        n = row["number"]
        path = RECORDS / (str(n) + ".json")
        if not path.is_file():
            continue
        r = json.loads(path.read_bytes())
        if r.get("instrumentation") != (3 if NATIVE else 2) or r["node"] != row["id"] or not r["valid"]:
            unknown.append(n)
            continue
        tracked.append(n)
        for s in r["good"]:
            proofs.setdefault(sites[s], []).append(n)
        for key, lo, hi in r.get("ranges", []):
            ranges.setdefault(sites[key], []).append([n, lo, hi])
        for module in r.get("verified_modules", ()):
            verified.setdefault(module, []).append(n)
        for site in r.get("visited", ()):
            visits.setdefault(sites[site], []).append(n)
        for p in r["reads"]:
            readers.setdefault(p, []).append(n)
    obj = {"version": 1, "nodes": [r["id"] for r in rows], "proofs": proofs, "reads": readers,
           "observed": tracked, "uncertain": unknown}
    if verified:
        obj["verified"] = verified
        obj["visits"] = visits
    if ranges:
        obj["ranges"] = ranges
    hashes = RECORDS / "hashes.json"
    if hashes.is_file():
        obj["hashes"] = json.loads(hashes.read_bytes())
    else:
        obj["hashes"] = {p.relative_to(BASE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (BASE / "sqlglot").rglob("*.py")}
    with tempfile.NamedTemporaryFile(dir=OUT.parent, delete=False) as tmp:
        with gzip.GzipFile(fileobj=tmp, mode="wb", filename="", mtime=0) as stream:
            stream.write(json.dumps(obj, separators=(",", ":")).encode())
        temp = Path(tmp.name)
    temp.chmod(0o644); temp.replace(OUT)
    print("index", len(tracked), "complete", len(unknown), "uncertain", len(proofs), "sites", flush=True)


def main():
    global RETRY_UNCERTAIN, REFRESH, REQUIRE_WITNESS, LEGACY_MINIMUM
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["build", "run", "index", "compile-native"])
    p.add_argument("--native", action="store_true")
    p.add_argument("--require-witness", action="store_true")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--retry-uncertain", action="store_true")
    p.add_argument("--ascending", action="store_true")
    p.add_argument("--skip", type=int, default=0)
    p.add_argument("--limit", type=int)
    p.add_argument("--ids", type=int, nargs="+")
    p.add_argument("--threshold", type=float, default=.3)
    p.add_argument("--workers", type=int, choices=(1, 2), default=1)
    args = p.parse_args()
    LEGACY_MINIMUM = args.threshold
    RETRY_UNCERTAIN = args.retry_uncertain
    REFRESH = args.refresh
    REQUIRE_WITNESS = args.require_witness
    if args.native:
        set_native()
    rows = json.loads(MANIFEST.read_bytes())["tests"]
    if args.cmd == "compile-native":
        compile_recorder()
    elif args.cmd == "build":
        build()
    elif args.cmd == "index":
        combine(rows)
    else:
        selected = [r for r in rows if (r["number"] in args.ids if args.ids else r["seconds"] >= args.threshold)]
        selected.sort(key=lambda r: r["seconds"], reverse=not args.ascending)
        selected = selected[args.skip:][:args.limit]
        with ThreadPoolExecutor(args.workers) as pool:
            for n, _ in enumerate(pool.map(test, selected), 1):
                if n % 16 == 0 or n == len(selected) or n == 1:
                    combine(rows)


if __name__ == "__main__":
    main()
