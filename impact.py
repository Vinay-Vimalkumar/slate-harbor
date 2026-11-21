"""Map changed statements and file reads to observed tests in the pinned tree."""
import ast
import hashlib
import json
import marshal
from pathlib import Path
from bisect import bisect_left, bisect_right
from functools import lru_cache
from types import CodeType

from owners import affected as binding_consumers
from source_reads import evaluation_spans
from test_edits import affected as changed_test_methods

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def executable(code):
    constants = tuple(executable(v) if isinstance(v, CodeType) else v for v in code.co_consts)
    return code.replace(co_consts=constants, co_firstlineno=1, co_linetable=b"")


@lru_cache(maxsize=40)
def base_code(source, path):
    return executable(compile(source, path, "exec", dont_inherit=True))


@lru_cache(maxsize=32)
def serialized_base(source, path):
    return hashlib.sha256(marshal.dumps(compile(source, path, "exec", dont_inherit=True))).digest()


@lru_cache(maxsize=40)
def syntax(source):
    tree = ast.dump(ast.parse(source, type_comments=True), annotate_fields=False)
    return hashlib.sha256(tree.encode()).digest()


@lru_cache(maxsize=32)
def prepared(path, source):
    dest = Path(__file__).with_name("prepared") / (path + ".bin")
    if dest.is_file():
        record = marshal.loads(dest.read_bytes())
        if record[0] == hashlib.sha256(source).digest():
            return record
    return None


@lru_cache(maxsize=1)
def position_sites():
    return json.loads(Path(__file__).with_name("position-readers.json").read_bytes())


class Catalog:
    def __init__(self, records, index, isolated=None, paths=None):
        self.rows = {row["number"]: row for row in records}
        self.all = set(self.rows)
        self.index = index
        self.isolated = isolated
        self.paths = paths
        self.by_file = {}
        for row in records:
            self.by_file.setdefault(row["file"], set()).add(row["number"])
        self.fallback = set(isolated.unknown) if isolated else set(index.get("fallback_tests", []))
        self.line_index = {}
        self.position_consumers = set()
        for path, sites in position_sites().items():
            for location, numbers in sites.items():
                self.position_consumers.update(numbers)
                if isolated:
                    self.position_consumers.update(isolated.at(path, int(location)))

    def pick(self, changes):
        chosen = set(self.fallback)
        for change in changes:
            if change.before == change.after and not change.mode_change:
                continue
            found = self._affected(change)
            chosen.update(found)
            if chosen == self.all:
                break
        return chosen & self.all

    def _affected(self, change):
        path = change.path
        observed = self.index["files"].get(path)
        readers = set(self.index.get("reads", {}).get(path, []))
        isolated = self.isolated
        if isolated:
            readers.update(isolated.reads(path))
        if change.mode_change:
            candidates = self.paths.mode_observers(path, isolated) if self.paths else None
            return self.all if candidates is None else candidates | readers
        if change.before is not None and self.paths and not path.endswith(".py"):
            observers = self.paths.content_observers(path, isolated, removed=change.after is None)
            if observers is not None:
                return readers | observers | set(observed or ())
        if change.before is None or change.after is None:
            watchers = self.paths.consumers(path) if self.paths else None
            if watchers is not None:
                found = readers | watchers
                if change.before is not None:
                    found.update(observed or ())
                    if self.isolated and path.endswith(".py"):
                        found.update(self.isolated.imports(path))
                return found
        if change.before is None:
            return self.all
        if change.after is None:
            return self._import_consumers(path, readers)

        if not path.endswith(".py"):
            return self.all if observed is None and not readers else set(observed or ()) | readers
        # A broken import changes collection outcomes regardless of recorded execution.
        try:
            new_code = compile(change.after, path, "exec", dont_inherit=True)
            prior = prepared(path, change.before)
            if executable(new_code) == (prior[1] if prior else base_code(change.before, path)):
                digest = hashlib.sha256(marshal.dumps(new_code)).digest()
                if digest == (prior[2] if prior else serialized_base(change.before, path)):
                    return readers
                return self._position_readers(path, readers)
            # Moving a class definition can change compiler-generated line
            # constants even when every source expression is identical.
            if (prior[3] if prior else syntax(change.before)) == syntax(change.after):
                return self._position_readers(path, readers)
            if path in self.by_file:
                test_users = changed_test_methods(change, self)
                if test_users is not None:
                    return test_users
            owners, statements = prior[4] if prior else structure(change.before)
        except (SyntaxError, ValueError, RecursionError):
            return self._import_consumers(path, readers)
        line_map = self.index["lines"].get(path, {})
        if path not in self.line_index:
            self.line_index[path] = sorted(int(k) for k in line_map)
        numbers = self.line_index[path]

        def in_range(start, end):
            selected = set()
            for line in numbers[bisect_left(numbers, start):bisect_right(numbers, end)]:
                selected.update(line_map[str(line)])
            if isolated:
                selected.update(isolated.span(path, start, end))
            return selected

        selected = set(readers)
        touched = change.old_lines | change.anchors
        if not touched:
            return self.all
        for line in touched:
            owner = next((o for o in owners if o[0] <= line <= o[1]), None)
            if owner is None:
                # Module/class definition-time edits can be consumed by code which
                # never executes the original line, even in a different test file.
                broad = self._import_consumers(path, readers)
                focused = binding_consumers(change, self)
                return broad if focused is None else (focused & broad) | readers
            start, end, function_name = owner
            # A test's code can change even if its new assertion is unreachable in
            # our measurement. Direct ownership also covers empty baseline traces.
            if path in self.by_file and function_name.startswith("test"):
                for n in self.by_file[path]:
                    if self.rows[n]["id"].rsplit("::", 1)[-1].split("[", 1)[0] == function_name:
                        selected.add(n)
            # A different short circuit can change a test which reached only an
            # earlier operand at baseline. Include the enclosing decision header.
            spans = prior[5].get(line, ()) if prior else evaluation_spans(change.before, line)
            for lo, hi in spans:
                selected.update(in_range(lo, hi))
            if line in change.old_lines:
                exact = set(line_map.get(str(line), ()))
                if isolated:
                    exact.update(isolated.at(path, line))
                if exact:
                    selected.update(exact)
                    continue
            stmt = next((s for s in statements if s[0] <= line <= s[1]), None)
            hit = in_range(*stmt) if stmt else set()
            if not hit:
                hit = in_range(start, end)
            if not hit and not (path in self.by_file and function_name.startswith("test")):
                return self.all if observed is None else set(observed) | readers
            selected.update(hit)
        return selected


    def _position_readers(self, path, readers):
        observers = self.position_consumers
        if self.isolated:
            observers = observers & self.isolated.imports(path)
        return readers | observers

    def _import_consumers(self, path, readers):
        if self.isolated:
            imported = self.isolated.imports(path)
            direct = self.by_file.get(path, set())
            if imported or direct:
                return readers | imported | direct
        return self.all


@lru_cache(maxsize=48)
def structure(source):
    tree = ast.parse(source)
    owners = []
    statements = []
    for node in ast.walk(tree):
        if isinstance(node, FUNCTIONS):
            # Decorators, defaults and annotations execute in the enclosing scope.
            first = node.body[0].lineno
            if first > node.lineno:
                owners.append((first, node.end_lineno, node.name))
        if isinstance(node, ast.stmt) and not isinstance(node, (*FUNCTIONS, ast.ClassDef)):
            statements.append((node.lineno, node.end_lineno))
    owners.sort(key=lambda row: row[1] - row[0])
    statements.sort(key=lambda row: row[1] - row[0])
    return owners, statements
