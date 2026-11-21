"""Trace simple fixture-to-class assignments and each actual load of that field."""
import ast
from collections import defaultdict
import concurrent.futures
import dis
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

LAB = Path(__file__).resolve().parents[1]
BASE = LAB / "source"
MANIFEST = LAB / "starter/sample/tests.json"
OUT = LAB.parent / "solution/setup-fixtures.json.gz"


def assignments():
    result = {}
    for file in (BASE / "tests").rglob("*.py"):
        source = file.read_bytes()
        if b"load_sql_fixture_pairs" not in source:
            continue
        tree = ast.parse(source)
        if not any(isinstance(n, ast.ImportFrom) and n.module == "tests.helpers"
                   and any(a.name == "load_sql_fixture_pairs" and not a.asname for a in n.names)
                   for n in tree.body):
            continue
        rel = file.relative_to(BASE).as_posix()
        for cls in (c for c in tree.body if isinstance(c,ast.ClassDef)):
            method = next((n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=="setUpClass"), None)
            if not method or len(method.args.args) != 1:
                continue
            for stmt in method.body:
                if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                    continue
                target, listing = stmt.targets[0], stmt.value
                if not (isinstance(target,ast.Attribute) and isinstance(target.value,ast.Name)
                        and target.value.id == method.args.args[0].arg and isinstance(listing,ast.Call)
                        and isinstance(listing.func,ast.Name) and listing.func.id=="list"
                        and not listing.keywords and len(listing.args)==1):
                    continue
                read = listing.args[0]
                if (isinstance(read,ast.Call) and isinstance(read.func,ast.Name)
                        and read.func.id=="load_sql_fixture_pairs" and not read.keywords and len(read.args)==1
                        and isinstance(read.args[0],ast.Constant) and isinstance(read.args[0].value,str)):
                    path = "tests/fixtures/"+read.args[0].value
                    result[path]={"file":rel,"class":cls.name,"field":target.attr,"line":stmt.lineno,
                                  "source_hash":hashlib.sha256(source).hexdigest()}
    return result


class Trace:
    def __init__(self, cfg, rows):
        self.cfg = cfg
        self.rows = {r["id"]:r["number"] for r in rows}
        self.fields = {c["field"] for c in cfg.values()}
        self.target_classes = {}
        self.active = None
        self.seen = defaultdict(set)
        self.other_reads = defaultdict(set)
        self.valid = set()
        self.ok = False
        self.by_code = {}
        self.prefix = str(BASE) + os.sep
        self.mon = sys.monitoring
        self.tool = self.mon.PROFILER_ID

    def start(self, code, offset):
        if code not in self.by_code:
            self.by_code[code] = {}
            if code.co_filename.startswith(self.prefix) and self.fields.intersection(code.co_names):
                self.by_code[code] = {op.offset:op.argval for op in dis.get_instructions(code)
                                      if op.opname in ("LOAD_ATTR","LOAD_SUPER_ATTR","LOAD_METHOD")
                                      and op.argval in self.fields}
            if self.by_code[code]:
                self.mon.set_local_events(self.tool, code, self.mon.events.INSTRUCTION)
        return self.mon.DISABLE

    def instruction(self, code, offset):
        attr = self.by_code[code].get(offset)
        if self.active is not None and attr:
            self.seen[attr].add(self.active)
        # Keep repeated calls active, including later numbered tests.

    def audit(self,event,args):
        if self.active is None or event != "open" or not isinstance(args[0],(bytes,str)):
            return
        filename = os.path.abspath(os.fsdecode(args[0]))
        if not filename.startswith(self.prefix):
            return
        relative = filename[len(self.prefix):]
        meta = self.cfg.get(relative)
        if not meta:
            return
        here = sys._getframe(1)
        if (here.f_code.co_name == "load_sql_fixture_pairs" and here.f_back
                and here.f_back.f_code.co_filename == self.prefix + meta["file"]
                and here.f_back.f_code.co_name == "setUpClass" and here.f_back.f_lineno == meta["line"]):
            return
        self.other_reads[relative].add(self.active)

    def pytest_collection_modifyitems(self,items):
        items[:] = [item for item in items if item.nodeid in self.rows]
        for item in items:
            for meta in self.cfg.values():
                if (item.cls and item.cls.__name__ == meta["class"]
                        and str(item.path) == self.prefix+meta["file"]):
                    self.target_classes[item.cls]=True
        for cls in self.target_classes:
            if "__getattribute__" in cls.__dict__ or "__getattr__" in cls.__dict__:
                raise ValueError("custom attribute lookup")
            original = cls.__getattribute__
            def trace(case, key, _original=original):
                if self.active is not None and key in self.fields:
                    self.seen[key].add(self.active)
                return _original(case,key)
            cls.__getattribute__=trace

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.ok = False

    @pytest.hookimpl(wrapper=True,tryfirst=True)
    def pytest_runtest_protocol(self,item):
        self.active = self.rows[item.nodeid]
        self.ok = True
        try:
            return (yield)
        finally:
            if self.ok:
                self.valid.add(self.active)
            self.active = None


def main():
    audit_only = "--audit-only" in sys.argv
    cfg = assignments()
    rows=json.loads(MANIFEST.read_bytes())["tests"]
    selected=[r for r in rows if any(r["id"].startswith(c["file"]+"::"+c["class"]+"::") for c in cfg.values())]
    if not selected:
        raise RuntimeError("no collected class-fixture consumers")
    probe=Trace(cfg,selected)
    spec=importlib.util.spec_from_file_location("observation_pool",LAB/"build-index.py")
    helper=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    os.chdir(BASE)
    sys.path.insert(0,str(BASE))
    sys.addaudithook(probe.audit)
    m, t=probe.mon,probe.tool
    m.use_tool_id(t,"setup fixture users")
    m.register_callback(t,m.events.PY_START,probe.start)
    m.register_callback(t,m.events.INSTRUCTION,probe.instruction)
    m.set_events(t,0 if audit_only else m.events.PY_START)
    with patch.object(concurrent.futures,"ProcessPoolExecutor",helper.InlinePool):
        status=pytest.main(["-q","-p","no:cacheprovider",*sorted({r["file"] for r in selected})],plugins=[probe])
    m.set_events(t,0)
    if status or len(probe.valid)!=len(selected):
        raise RuntimeError("unverified baseline")
    if audit_only:
        cfg = json.loads(gzip.decompress(OUT.read_bytes()))["fixtures"]
    for path,c in cfg.items():
        group={r["number"] for r in selected if r["id"].startswith(c["file"]+"::"+c["class"]+"::")}
        c["setup_only"]=sorted(group-probe.other_reads[path])
        if not audit_only:
            c["unused"]=sorted(group-probe.seen[c["field"]]-probe.other_reads[path])
    result={"fixtures":cfg,"nodes":[r["id"] for r in rows]}
    with gzip.GzipFile(filename=str(OUT),mode="wb",mtime=0) as f:
        f.write(json.dumps(result,separators=(",",":")).encode())
    print({p:len(c["unused"]) for p,c in cfg.items()})


if __name__=="__main__":
    main()
