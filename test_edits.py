"""Limit a test-body-only edit to its collected instances and observed consumers."""
import ast
import gzip
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from types import CodeType

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@lru_cache(maxsize=1)
def profile():
    path = Path(__file__).with_name("test-edits.json.gz")
    if not path.is_file():
        return {}
    with gzip.open(path, "rt") as source:
        return json.load(source)


@lru_cache(maxsize=2)
def catalog_matches(catalog, expected):
    ids = "\n".join(catalog.rows[n]["id"] for n in sorted(catalog.rows))
    return hashlib.sha256(ids.encode()).hexdigest() == expected


@lru_cache(maxsize=6)
def structures(source, path):
    tree = ast.parse(source, path, type_comments=True)
    bodies = {}
    lines = source.splitlines(keepends=True)
    standard_unittest = any(isinstance(node, ast.Import) and any(alias.name == "unittest" and not alias.asname
                            for alias in node.names) for node in tree.body)
    if standard_unittest:
        standard_unittest = not any(isinstance(node, ast.Name) and node.id == "unittest"
                                    and isinstance(node.ctx, (ast.Store, ast.Del)) for node in ast.walk(tree))

    def plain_decorator(decorator):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        return (standard_unittest and isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == "unittest" and target.attr in {"skip", "skipIf", "skipUnless", "expectedFailure"})

    def visit(scope, prefix):
        for node in scope.body:
            if isinstance(node, ast.ClassDef) and not node.decorator_list and not node.keywords:
                visit(node, (*prefix, node.name))
            elif (isinstance(node, FUNCTIONS) and node.name.startswith("test")
                  and all(plain_decorator(d) for d in node.decorator_list) and node.body[0].lineno > node.lineno):
                qualified = ".".join((*prefix, node.name))
                # Multiple definitions with the same name retain their physical location.
                same_name = bodies.setdefault(qualified, [])
                first = node.body[0].lineno
                code_line = node.decorator_list[0].lineno if node.decorator_list else node.lineno
                definition = (code_line, first, node.end_lineno, ast.get_docstring(node, clean=False),
                              b"".join(lines[first - 1:node.end_lineno]))
                same_name.append(definition)
                node.body = [ast.Pass()]

    visit(tree, ())
    return ast.dump(tree, annotate_fields=False), bodies, compile(source, path, "exec", dont_inherit=True)


def outside(code, changed):
    # The class body itself may change for an edit inside a method, for instance
    # __classcell__ and Python 3.13's compiler-generated __static_attributes__.
    if (code.co_qualname, code.co_firstlineno) in changed:
        return ("test", code.co_qualname, code.co_flags, code.co_freevars,
                code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount)
    constants = tuple(outside(c, changed) if isinstance(c, CodeType) else c for c in code.co_consts)
    return code.replace(co_consts=constants, co_firstlineno=1, co_linetable=b"")


def affected(change, catalog):
    path = change.path
    if (catalog.isolated is None or path not in catalog.by_file or not path.endswith(".py")
            or not change.before or not change.after):
        return None
    data = profile()
    if (data.get("files", {}).get(path) != hashlib.sha256(change.before).hexdigest()
            or not catalog_matches(catalog, data["catalog"])):
        return None
    try:
        old_shell, old, old_code = structures(change.before, path)
        new_shell, new, new_code = structures(change.after, path)
    except (SyntaxError, ValueError, RecursionError):
        return None
    if old_shell != new_shell or old.keys() != new.keys():
        return None
    changed_old, changed_new = set(), set()
    verify_rewrite = False
    readers = set(data["readers"].get(path, ())) | set(catalog.index.get("reads", {}).get(path, ()))
    index_lines = catalog.index["lines"].get(path, {})
    isolated = catalog.isolated
    for qualified, original in old.items():
        revised = new[qualified]
        if len(original) != len(revised):
            return None
        for (line, first, end, doc, body), (new_line, _, _, new_doc, new_body) in zip(original, revised):
            if body == new_body:
                continue
            ids = data["definitions"][path].get(f"{qualified}@{line}")
            if not ids or doc != new_doc:
                return None
            changed_old.add((qualified, line))
            changed_new.add((qualified, new_line))
            verify_rewrite |= re.search(rb"\bassert\b", new_body) is not None
            readers.update(ids)
            # Include direct, inherited, aliased and dynamic calls into the method.
            for number in range(first, end + 1):
                readers.update(index_lines.get(str(number), ()))
            readers.update(isolated.span(path, first, end))
    if not changed_old or outside(old_code, changed_old) != outside(new_code, changed_new):
        return None
    # A newly invalid assertion rewrite would prevent the whole file from collecting.
    if verify_rewrite:
        from _pytest.assertion.rewrite import rewrite_asserts
        try:
            tree = ast.parse(change.after, path)
            rewrite_asserts(tree, change.after, path)
            compile(tree, path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError, RecursionError, AssertionError):
            return None
    readers.update(isolated.reads(path) - set(data["rewrite_only"].get(path, ())))
    return catalog._position_readers(path, readers)
