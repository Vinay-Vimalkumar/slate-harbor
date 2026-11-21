"""Recognize pure comparison edits and query equivalent outcomes from baseline tests."""
import ast
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path


OPPOSITE = {"Is": "Eq", "Eq": "Is", "IsNot": "NotEq", "NotEq": "IsNot",
            "Lt": "LtE", "LtE": "Lt", "Gt": "GtE", "GtE": "Gt"}
NUMERIC = {"Eq", "NotEq", "Lt", "LtE", "Gt", "GtE"}
SPELLINGS = {"Is": [b"is"], "Eq": [b"=="], "IsNot": [b"is", b"not"], "NotEq": [b"!="],
             "Lt": [b"<"], "LtE": [b"<="], "Gt": [b">"], "GtE": [b">="]}


def operator_span(source, offsets, node, operation):
    left, right = node.left, node.comparators[0]
    if left.end_lineno != right.lineno:
        return None
    offset = offsets[right.lineno - 1]
    lo, hi = offset + left.end_col_offset, offset + right.col_offset
    if source[lo:hi].split() != SPELLINGS[operation]:
        return None
    return lo, hi


def integer_literal(expr):
    return isinstance(expr, ast.Constant) and type(expr.value) is int and -(2**63) <= expr.value < 2**63


def identity_operand(expr):
    # Keep compiler diagnostics unchanged; constant expressions can warn on "is".
    if isinstance(expr, ast.Constant):
        return expr.value is None or expr.value is Ellipsis or type(expr.value) is bool
    return isinstance(expr, (ast.Name, ast.Attribute, ast.Call, ast.Subscript))


def remove_operators(source, spans):
    for lo, hi in sorted(spans, reverse=True):
        source = source[:lo] + b"\0OP\0" + source[hi:]
    return source


@lru_cache(maxsize=80)
def substitutions(path, before, after):
    trees = [ast.parse(s) for s in (before, after)]
    old_sites = [n for n in ast.walk(trees[0]) if isinstance(n, ast.Compare)]
    new_sites = [n for n in ast.walk(trees[1]) if isinstance(n, ast.Compare)]
    if len(old_sites) != len(new_sites):
        return ()
    offsets = []
    for source in (before, after):
        starts = [0]
        starts.extend(i+1 for i, byte in enumerate(source) if byte == 10)
        offsets.append(starts)
    changed = []
    spans = ([], [])
    for a, b in zip(old_sites, new_sites):
        if len(a.ops) != 1 or len(b.ops) != 1:
            continue
        old = type(a.ops[0]).__name__
        new = type(b.ops[0]).__name__
        site = f"{path}:{a.lineno}:{a.col_offset}:{old}"
        if OPPOSITE.get(old) == new:
            if old in {"Eq", "Is", "NotEq", "IsNot"} and not all(map(identity_operand, (a.left, a.comparators[0]))):
                return ()
            for i, (node, source) in enumerate(((a, before), (b, after))):
                span = operator_span(source, offsets[i], node, type(node.ops[0]).__name__)
                if span is None:
                    return ()
                spans[i].append(span)
            changed.append(site)
            b.ops[0] = a.ops[0]
        elif old == new and old in NUMERIC:
            original = (a.left, a.comparators[0])
            updated = (b.left, b.comparators[0])
            if sum(map(integer_literal, original)) != 1 or sum(map(integer_literal, updated)) != 1:
                continue
            for x, y in zip(original, updated):
                if not (integer_literal(x) and integer_literal(y)) or x.value == y.value:
                    continue
                for i, v in enumerate((x, y)):
                    start = offsets[i][v.lineno - 1]
                    spans[i].append((start + v.col_offset, start + v.end_col_offset))
                changed.append(site + "\0" + str(y.value))
                y.value = x.value
    if not changed or remove_operators(before, spans[0]) != remove_operators(after, spans[1]):
        return ()
    if ast.dump(trees[0]) != ast.dump(trees[1]):
        return ()
    return tuple(changed)


class Relations:
    def __init__(self, index):
        self.proofs = index["proofs"]
        self.reads = index["reads"]
        self.ranges = index.get("ranges", {})
        self.hashes = index.get("hashes", {})

    @classmethod
    def load(cls, path, rows):
        if not Path(path).is_file():
            return None
        with gzip.open(path, "rt") as f:
            index = json.load(f)
        if index["version"] != 1 or index["nodes"] != [r["id"] for r in rows]:
            return None
        extended = Path(path).with_name("relation-order-index.json.gz")
        if extended.is_file() and extended != Path(path):
            with gzip.open(extended, "rt") as f:
                other = json.load(f)
            if other.get("version") == 1 and other.get("nodes") == index["nodes"]:
                for key in ("reads", "proofs"):
                    for feature, ids in other[key].items():
                        index[key].setdefault(feature, []).extend(ids)
                index["ranges"] = other.get("ranges", {})
                index["hashes"] = other.get("hashes", {})
        return cls(index)

    def proven(self, changes, candidates, *, readers=()):
        """Return measured pass IDs. Preserve any additional caller-supplied readers."""
        out = set(candidates) - set(readers)
        for change in changes:
            if not change.path.startswith("sqlglot/") or not change.path.endswith(".py"):
                return set()
            if change.before is None or change.after is None:
                return set()
            if self.hashes and self.hashes.get(change.path) != hashlib.sha256(change.before).hexdigest():
                return set()
            try:
                sites = substitutions(change.path, change.before, change.after)
            except (SyntaxError, ValueError, RecursionError):
                return set()
            if not sites:
                return set()
            out.difference_update(self.reads.get(change.path, []))
            for site in sites:
                key, sep, value = site.partition("\0")
                if sep:
                    target = int(value)
                    out.intersection_update(n for n, lo, hi in self.ranges.get(key, []) if lo <= target <= hi)
                else:
                    out.intersection_update(self.proofs.get(key, []))
        return out if changes else set()
