"""Build a class-owner map from the pinned baseline without consulting changes or labels."""
import argparse
import ast
from collections import defaultdict
from concurrent import futures
from functools import lru_cache
import gzip
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import pytest

LAB = Path(__file__).resolve().parent
SOURCE = LAB / "source"
MANIFEST = LAB / "starter/sample/tests.json"
REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
spec = importlib.util.spec_from_file_location("build_index_helpers", LAB / "build-index.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


@lru_cache(maxsize=16384)
def owner_names(cls):
    owners = set()

    def register(value):
        if isinstance(value, type):
            for parent in value.__mro__:
                if parent.__module__.startswith("sqlglot.") and "<locals>" not in parent.__qualname__:
                    owners.add(parent.__module__ + "." + parent.__qualname__)

    register(cls)
    # The dialect metaclass materializes parser, generator and tokenizer configuration
    # once, at class creation. Include the receiver family even if execution then only
    # consults generated tables or class-level values without constructing that helper.
    if any(base.__module__ == "sqlglot.dialects.dialect" and base.__name__ == "Dialect"
           for base in cls.__mro__):
        for name in ("parser_class", "generator_class", "tokenizer_class", "jsonpath_tokenizer_class"):
            register(getattr(cls, name, None))
    return owners


class Trace:
    def __init__(self, catalog, records, todo):
        self.ids = {row["id"]: row["number"] for row in catalog}
        self.records = records
        self.todo = todo
        self.active = False
        self.prefix = str(SOURCE / "sqlglot") + "/"
        self.monitor = sys.monitoring
        self.tool = self.monitor.COVERAGE_ID

    @lru_cache(maxsize=131072)
    def kind(self, code):
        if not code.co_filename.startswith(self.prefix) or not code.co_argcount:
            return None
        arg = code.co_varnames[0]
        if arg == "cls" or (arg == "self" and code.co_name in ("__init__", "__new__")):
            return arg
        return None

    def start(self, code, offset):
        arg = self.kind(code)
        if not arg:
            return self.monitor.DISABLE
        if not self.active:
            return
        val = sys._getframe(1).f_locals.get(arg)
        if val is None:
            return
        cls = val if isinstance(val, type) else type(val)
        self.classes.add(cls)
        if code.co_name == "classes" and code.co_filename.endswith("/sqlglot/dialects/dialect.py"):
            self.registry = True

    def returned(self, code, offset, result):
        if code.co_name not in ("get_or_raise", "get", "__getitem__") or not code.co_filename.endswith("/sqlglot/dialects/dialect.py"):
            return self.monitor.DISABLE
        if self.active and result is not None:
            self.classes.add(result if isinstance(result, type) else type(result))

    def pytest_collection_modifyitems(self, session, config, items):
        missing = set(self.ids) - {item.nodeid for item in items}
        if missing:
            raise RuntimeError(f"{len(missing)} catalog tests missing at collection")
        items[:] = [item for item in items if self.ids.get(item.nodeid) in self.todo]

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.valid = False

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        number = self.ids[item.nodeid]
        self.classes = set()
        self.registry = False
        self.valid = True
        self.active = True
        try:
            return (yield)
        finally:
            self.active = False
            names = set()
            for cls in self.classes:
                names.update(owner_names(cls))
            dest = self.records / f"{number}.json"
            tmp = dest.with_suffix(".tmp")
            tmp.write_text(json.dumps({"id": item.nodeid, "valid": self.valid,
                                       "registry_enumeration": self.registry, "owners": sorted(names)}))
            tmp.replace(dest)

    def run(self):
        m, tool = self.monitor, self.tool
        m.use_tool_id(tool, "baseline-owners")
        m.register_callback(tool, m.events.PY_START, self.start)
        m.register_callback(tool, m.events.PY_RETURN, self.returned)
        m.set_events(tool, m.events.PY_START | m.events.PY_RETURN)
        try:
            with patch.object(futures, "ProcessPoolExecutor", helpers.InlinePool):
                status = pytest.main(["-p","no:cacheprovider","-p","no:terminal"], plugins=[self])
        finally:
            m.set_events(tool, 0)
            m.register_callback(tool, m.events.PY_START, None)
            m.register_callback(tool, m.events.PY_RETURN, None)
            m.free_tool_id(tool)
        return int(status)


def static_consumers(catalog, owners):
    """Find direct class references in tests and simple closure-producing functions."""
    by_file = defaultdict(list)
    for row in catalog:
        by_file[row["file"]].append(row["number"])
    short_names = {owner.rsplit(".", 1)[-1] for owner in owners}
    direct = defaultdict(set)
    for path, tests in by_file.items():
        tree = ast.parse((SOURCE / path).read_bytes())
        aliases = {a.asname: a.name for item in ast.walk(tree) if isinstance(item, ast.ImportFrom)
                   for a in item.names if a.asname}
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
        names.update(aliases[n] for n in tuple(names) if n in aliases)
        for name in names & short_names:
            direct[name].update(tests)

    definitions = {}
    links = defaultdict(dict)
    for path in sorted((SOURCE / "sqlglot").rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        module = relative[:-3].replace("/", ".").removesuffix(".__init__")
        tree = ast.parse(path.read_bytes())
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for a in node.names:
                    links[relative][a.asname or a.name] = f"{node.module}.{a.name}"
            if not isinstance(node, ast.FunctionDef) or node.decorator_list:
                continue
            body = node.body
            if ast.get_docstring(node) is not None:
                body = body[1:]
            if not body or not isinstance(body[-1], ast.Return):
                continue
            result = body[-1].value
            if len(body) == 1 and isinstance(result, ast.Lambda):
                params, first, last = result.args, result.body.lineno, result.body.end_lineno
            elif (len(body) == 2 and isinstance(body[0], ast.FunctionDef)
                    and not body[0].decorator_list and isinstance(result, ast.Name)
                    and result.id == body[0].name):
                fn = body[0]
                params, first, last = fn.args, fn.body[0].lineno, fn.end_lineno
                eager_names = {n.id for ann in ([fn.returns] + [a.annotation for a in
                               [*params.args, *params.posonlyargs, *params.kwonlyargs]])
                               if ann is not None for n in ast.walk(ann) if isinstance(n, ast.Name)}
                outer_names = {a.arg for a in [*node.args.args, *node.args.posonlyargs, *node.args.kwonlyargs,
                                                node.args.vararg, node.args.kwarg] if a is not None}
                if eager_names & outer_names:
                    continue
            else:
                continue
            if params.defaults or any(d is not None for d in params.kw_defaults):
                continue
            full_name = f"{module}.{node.name}"
            definitions[full_name] = [relative, first, last]
            links[relative][node.name] = full_name
    factories = {path: {local: definitions[src] for local, src in imported.items() if src in definitions}
                 for path, imported in links.items()}
    return {n: sorted(v) for n, v in direct.items()}, {p: v for p, v in factories.items() if v}


def symbol_consumers(coverage, owner_names):
    """Join uppercase attribute read locations to baseline per-test line coverage."""
    uses = defaultdict(set)
    eager = set()
    eager_copies = set()
    classes = {name.rsplit(".", 1)[-1] for name in owner_names}
    scalar_names = set()
    for top in ("sqlglot", "tests"):
        for path in (SOURCE / top).rglob("*.py"):
            lines = coverage["lines"].get(path.relative_to(SOURCE).as_posix(), {})
            tree = ast.parse(path.read_bytes())
            def scan(node, in_function, inherited=()):
                if isinstance(node, ast.ClassDef) and not in_function:
                    for item in node.body:
                        if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                            if isinstance(item.value, (ast.Constant, ast.UnaryOp)) and item.targets[0].id.isupper():
                                scalar_names.add(item.targets[0].id)
                name = (node.attr if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                        else node.value if isinstance(node, ast.Constant) and isinstance(node.value, str)
                        else None)
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and not in_function:
                    target = node.value
                    owner = target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else None
                    if owner in classes and owner not in inherited:
                        eager_copies.add(owner + "." + node.attr)
                if name and name.isidentifier() and name.isupper():
                    if in_function:
                        uses[name].update(lines.get(str(node.lineno), ()))
                    else:
                        eager.add(name)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    body = node.body if isinstance(node.body, list) else [node.body]
                    for child in ast.iter_child_nodes(node):
                        scan(child, in_function or child in body, inherited)
                elif isinstance(node, ast.ClassDef):
                    bases = {base.id if isinstance(base, ast.Name) else base.attr
                             for base in node.bases if isinstance(base, (ast.Name, ast.Attribute))}
                    for child in ast.iter_child_nodes(node):
                        scan(child, in_function, bases if child in node.body else inherited)
                else:
                    for child in ast.iter_child_nodes(node):
                        scan(child, in_function, inherited)
            scan(tree, False)
    members = {name: sorted(hits) for name, hits in uses.items() if hits and name in scalar_names and name not in eager}
    return members, sorted(eager_copies)


def aggregate(catalog, records, dest):
    owners = defaultdict(list)
    fallback = []
    observed, valid_count = 0, 0
    enumeration = []
    for row in catalog:
        n = row["number"]
        path = records / f"{n}.json"
        if not path.is_file():
            fallback.append(n)
            continue
        record = json.loads(path.read_text())
        if record["id"] != row["id"]:
            raise ValueError("Mismatched catalog")
        observed += 1
        valid_count += int(record["valid"])
        if not record["valid"]:
            fallback.append(n)
        if record["registry_enumeration"]:
            enumeration.append(n)
        for name in record["owners"]:
            owners[name].append(n)
    direct, factories = static_consumers(catalog, owners)
    with gzip.open(LAB.parent / "solution/coverage-index.json.gz", "rt") as src:
        members, eager_copies = symbol_consumers(json.load(src), owners)
    result = {"version":1, "revision":REVISION, "test_count":len(catalog), "observed_tests":observed,
              "passing_tests":valid_count, "fallback_tests":fallback, "registry_enumeration":enumeration,
              "direct_tests":direct, "factories":factories, "members":members,
              "eager_copies":eager_copies, "owners":dict(sorted(owners.items()))}
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
        with gzip.GzipFile(fileobj=tmp,mode="wb",filename="",mtime=0) as gz:
            gz.write(json.dumps(result,separators=(",",":")).encode())
        tmp_name = Path(tmp.name)
    tmp_name.chmod(0o644)
    tmp_name.replace(dest)
    print(f"{observed}/{len(catalog)} recorded, {len(owners)} owners, {len(fallback)} fallback, {dest.stat().st_size} bytes.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["run","index"])
    ap.add_argument("--records", type=Path, required=True)
    ap.add_argument("--start", type=int,default=0)
    ap.add_argument("--end",type=int,default=1620)
    ap.add_argument("--output",type=Path)
    a=ap.parse_args()
    catalog=json.loads(MANIFEST.read_bytes())["tests"]
    records=a.records.resolve()
    records.mkdir(parents=True,exist_ok=True)
    if a.mode=="index":
        aggregate(catalog,records,a.output)
    else:
        todo={r["number"] for r in catalog if a.start <= r["number"] < a.end
              and not (records / f"{r['number']}.json").is_file()}
        if todo:
            os.chdir(SOURCE)
            sys.path.insert(0,str(SOURCE))
            os.environ["PATH"]=str(Path(sys.executable).parent)+os.pathsep+os.environ.get("PATH","")
            raise SystemExit(Trace(catalog,records,todo).run())


if __name__=="__main__":
    main()
