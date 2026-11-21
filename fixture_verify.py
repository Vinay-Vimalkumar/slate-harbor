"""Verify independent optimizer fixture cases using the pinned test's own loader."""
import ast
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import io
from pathlib import Path
import runpy
from time import monotonic
from types import FunctionType


HELPERS_SHA = "c0dc285fccd1b8bee4a451f5962118fe771de005843b56e3af4c7136f74c3ed3"
TEST_SHA = "35c02d71cacfa5d8e24319ef245963da3c1151b891281bac1864a00fa183cbf7"


@dataclass
class Assessment:
    passed: set = field(default_factory=set)
    failed: set = field(default_factory=set)
    # A strict subset of candidates can be tested against an equivalent, smaller fixture.
    reduced: dict = field(default_factory=dict)
    eligible: list = field(default_factory=list)


@lru_cache(maxsize=2)
def _pinned(base):
    hp = base / "tests/helpers.py"
    tp = base / "tests/test_optimizer.py"
    if hashlib.sha256(hp.read_bytes()).hexdigest() != HELPERS_SHA or hashlib.sha256(tp.read_bytes()).hexdigest() != TEST_SHA:
        return None
    helpers = runpy.run_path(str(hp))
    tree = ast.parse(tp.read_bytes())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TestOptimizer")
    consumers = {}
    for method in cls.body:
        if not isinstance(method, ast.FunctionDef) or method.decorator_list:
            continue
        # Only a literal unfiltered check_file call with an unconditional path.
        for stmt in method.body:
            call = stmt.value if isinstance(stmt, ast.Expr) else None
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if not isinstance(call.func.value, ast.Name) or call.func.value.id != "self" or call.func.attr != "check_file":
                continue
            if not call.args or not isinstance(call.args[0], ast.Constant) or not isinstance(call.args[0].value, str):
                continue
            if any(k.arg in (None, "only") for k in call.keywords) or len(call.args) > 2:
                continue
            name = "tests/fixtures/optimizer/" + call.args[0].value + ".sql"
            nodeid = f"tests/test_optimizer.py::TestOptimizer::{method.name}"
            # A partial run must have no other assertions/setup in the test body,
            # no mutable database execution and no conditional case filtering.
            standalone = len(method.body) == 1 and all(k.arg != "execute" for k in call.keywords)
            consumers.setdefault(name, {})[nodeid] = standalone
    return helpers, consumers


def _rows(helpers, data):
    loader = helpers["load_sql_fixture_pairs"]
    def read(*args, **kwargs):
        return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")
    scope = dict(loader.__globals__, open=read)
    return list(FunctionType(loader.__code__, scope)("input.sql"))


def classify(changes, candidates, *, base=Path("/opt/base")):
    plan = Assessment()
    if len(changes) != 1:
        return plan
    path, after = next(iter(changes.items()))
    if not path.startswith("tests/fixtures/optimizer/") or not path.endswith(".sql") or not isinstance(after, bytes):
        return plan
    try:
        base = Path(base).resolve()
        pinned = _pinned(base)
        if not pinned:
            return plan
        helpers, consumers = pinned
        uses = consumers.get(path, {})
        selected = [r for r in candidates if r["id"] in uses]
        if not selected:
            return plan
        old = _rows(helpers, (base / path).read_bytes())
        new = _rows(helpers, after)
        if len(old) != len(new):
            return plan
        changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
        if not changed:
            plan.passed = {r["number"] for r in selected if uses[r["id"]]}
            return plan
        # The test compares the formatted result literally. With the complete
        # original inputs/metadata intact the same value cannot equal two outputs.
        if all(old[i][:2] == new[i][:2] for i in changed):
            plan.failed = {r["number"] for r in selected}
            return plan
        if any(meta.get("execute") or meta.get("execute_min_python") for meta, _, _ in old + new):
            return plan
        if any(old[i][0] != new[i][0] for i in changed) or len(changed) == len(new):
            return plan
        plan.eligible = [r for r in selected if uses[r["id"]]]
        if plan.eligible:
            # Take the original revised text spans and check the actual loader
            # reproduces each changed (metadata, input, expected) tuple exactly.
            parts = helpers["_filter_comments"](after.decode("utf-8")).split(";")
            data = ";".join(p for i in changed for p in parts[2*i:2*i+2]) + ";"
            reduced = data.encode("utf-8")
            if _rows(helpers, reduced) != [new[i] for i in changed]:
                return Assessment()
            plan.reduced = {path: reduced}
        return plan
    except (OSError, UnicodeError, ValueError, KeyError, IndexError, StopIteration, SyntaxError):
        return Assessment()


def verify(changes, candidates, *, deadline, base=Path("/opt/base")):
    if monotonic() >= deadline:
        return set()
    plan = classify(changes, candidates, base=base)
    if plan.eligible and monotonic() < deadline:
        from fast_verify import verify as run
        plan.passed.update(run(plan.reduced, plan.eligible, deadline=deadline, base=base))
    return plan.passed
