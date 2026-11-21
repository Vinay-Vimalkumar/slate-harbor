"""Narrow supported changes in class configuration and deferred lambda bodies."""
import ast
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path
from lambda_sites import literal_arguments


@lru_cache(maxsize=1)
def _observations():
    with gzip.open(Path(__file__).with_name("owner-index.json.gz"), "rt") as src:
        return json.load(src)


@lru_cache(maxsize=1)
def _direct():
    path = Path(__file__).with_name("direct-index.json.gz")
    if path.is_file():
        with gzip.open(path, "rt") as src:
            data = json.load(src)
        owner = path.with_name("owner-index.json.gz")
        if hashlib.sha256(owner.read_bytes()).hexdigest() == data["owner_sha"]:
            return data["direct"]
    return {}


def _signature(node):
    return ast.dump(node, annotate_fields=False)


@lru_cache(maxsize=16)
def _bindings(source):
    """Separate import-time value expressions from module/class definitions."""
    tree = ast.parse(source)
    values = {}

    def scan(scope, prefix):
        assignments = {}
        for stmt in scope.body:
            if isinstance(stmt, ast.ClassDef):
                scan(stmt, (*prefix, stmt.name))
                continue
            if isinstance(stmt, ast.AnnAssign):
                target = stmt.target
            elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
            else:
                continue
            if not isinstance(target, ast.Name) or stmt.value is None:
                continue
            key = (*prefix, target.id)
            if key in assignments:
                # Keep both original values in the shape signature. A different
                # unambiguous binding in the same class can still be analyzed.
                if key in values:
                    assignments[key].value = values.pop(key)
                continue
            assignments[key] = stmt
            values[key] = stmt.value
            stmt.value = ast.Constant(value=None)

    scan(tree, ())
    return _signature(tree), values


def _literal_or_equal(a, b):
    if _signature(a) == _signature(b):
        return True
    try:
        x, y = ast.literal_eval(a), ast.literal_eval(b)
    except (SyntaxError, ValueError, TypeError):
        return False
    return type(x) is type(y) and type(x) in (bool, int, str, float, bytes)


def _modified_values(old, new, found, factories):
    """Report changed leaf nodes; dictionary definitions/keys must stay intact."""
    if _signature(old) == _signature(new):
        return True
    if isinstance(old, ast.Lambda) and isinstance(new, ast.Lambda):
        if _signature(old.args) != _signature(new.args):
            return False
        found.append(("lambda", (old,new)))
        return True
    if (isinstance(old, ast.Call) and isinstance(new, ast.Call)
            and isinstance(old.func, ast.Name) and isinstance(new.func, ast.Name)
            and old.func.id == new.func.id and old.func.id in factories
            and len(old.args) == len(new.args)
            and [kw.arg for kw in old.keywords] == [kw.arg for kw in new.keywords]
            and all(_literal_or_equal(a, b) for a, b in zip(old.args, new.args))
            and all(_literal_or_equal(a.value, b.value) for a, b in zip(old.keywords, new.keywords))):
        found.append(("factory", factories[old.func.id]))
        return True
    if isinstance(old, ast.Dict) and isinstance(new, ast.Dict) and len(old.keys) == len(new.keys):
        for k, other, value, replacement in zip(old.keys, new.keys, old.values, new.values):
            if ((k is None) != (other is None) or
                    (k is not None and _signature(k) != _signature(other)) or
                    not _modified_values(value, replacement, found, factories)):
                return False
        return True
    if isinstance(old, (ast.Tuple, ast.List)) and type(old) is type(new) and len(old.elts) == len(new.elts):
        return all(_modified_values(a, b, found, factories) for a, b in zip(old.elts, new.elts))
    try:
        a, b = ast.literal_eval(old), ast.literal_eval(new)
    except (ValueError, TypeError, SyntaxError, OverflowError):
        return False
    if type(a) is not type(b) or type(a) not in (str, int, bool, float, bytes, type(None)):
        return False
    found.append(("constant", old))
    return True


def affected(change, catalog):
    """Return candidates when every change is a recognized binding edit, else None."""
    if not (change.path.startswith("sqlglot/") and change.path.endswith(".py")
            and change.before is not None and change.after is not None):
        return None
    try:
        old_shape, old = _bindings(change.before)
        new_shape, new = _bindings(change.after)
        if old_shape != new_shape or old.keys() != new.keys():
            return None
        index = _observations()
    except (SyntaxError, ValueError, OSError, RecursionError):
        return None
    if index["revision"] != catalog.index.get("revision") or index["test_count"] != len(catalog.all):
        return None
    module = change.path[:-3].replace("/", ".").removesuffix(".__init__")
    selected = set(index["fallback_tests"]) | catalog.fallback
    selected.update(index["registry_enumeration"])
    selected.update(catalog.index.get("reads", {}).get(change.path, ()))
    line_hits = catalog.index["lines"].get(change.path, {})
    matched = False
    for key, expr in old.items():
        replacement = new[key]
        if expr is None or replacement is None:
            return None
        if _signature(expr) == _signature(replacement):
            continue
        matched = True
        changes = []
        if not _modified_values(expr, replacement, changes, index.get("factories", {}).get(change.path, {})) or not changes:
            return None
        owner = ".".join((module, *key[:-1]))
        candidates = index["owners"].get(owner)
        for kind, leaf in changes:
            if kind == "factory":
                path, start, end = leaf
                lines = catalog.index["lines"].get(path, {})
                invoked = {n for line in range(start, end + 1) for n in lines.get(str(line), ())}
                # A factory can be shared between independent classes; the line
                # trace includes every observed invocation of the returned body.
                if invoked:
                    selected.update(invoked)
                    continue
            if kind == "lambda":
                before, after = leaf
                required = literal_arguments(change.path, change.before, before, after)
                if required is not None and catalog.isolated and not catalog.isolated.unknown:
                    for line in required:
                        selected.update(line_hits.get(str(line), ()))
                        selected.update(catalog.isolated.at(change.path, line))
                    continue
                # The body executes on invocation. Function creation also evaluates
                # at some of these lines; including that observation is conservative.
                traced = set()
                for line in range(before.body.lineno, before.body.end_lineno + 1):
                    traced.update(line_hits.get(str(line), ()))
                if traced:
                    selected.update(traced)
                    continue
            if candidates is None or len(key) == 1 or key[-1].startswith("__"):
                return None
            if ".".join(key[-2:]) in index.get("eager_copies", ()):
                # Another module or class reads the value while defining itself.
                # Its future users can observe a copy without touching this owner.
                return None
            precise = index.get("members", {}).get(key[-1])
            if kind == "constant" and leaf is expr and precise is not None:
                selected.update(set(candidates).intersection(precise))
            else:
                selected.update(candidates)
            original = index.get("direct_tests", {}).get(key[-2], ())
            selected.update(_direct().get(key[-2], original))
    return selected if matched else None
