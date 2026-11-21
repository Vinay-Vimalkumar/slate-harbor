"""Recognize pure comparison edits and query equivalent outcomes from baseline tests."""
import ast
from functools import lru_cache
import gzip
import hashlib
import json
import re
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


def integer_value(expr):
    sign = 1
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, (ast.USub, ast.UAdd)):
        sign, expr = (-1 if isinstance(expr.op, ast.USub) else 1), expr.operand
    if isinstance(expr, ast.Constant) and type(expr.value) is int:
        value = sign * expr.value
        if -(2**63) <= value < 2**63:
            return value
    return None


def identity_operand(expr):
    # Keep compiler diagnostics unchanged; constant expressions can warn on "is".
    if isinstance(expr, ast.Constant):
        return expr.value is None or expr.value is Ellipsis or type(expr.value) is bool
    return isinstance(expr, (ast.Name, ast.Attribute, ast.Call, ast.Subscript))


def remove_operators(source, spans):
    for lo, hi in sorted(spans, reverse=True):
        source = source[:lo] + b"\0OP\0" + source[hi:]
    return source


TOKENS = re.compile(rb"\bis(?:[ \t]+not)?\b|[=!<>]=|[<>]|[+-]?\b[0-9][0-9a-fA-FxXoObB_]*\b")


def shape(line):
    return TOKENS.sub(b"?", line).translate(None, b" \t")


@lru_cache(maxsize=80)
def substitutions(path, before, after):
    old_lines, new_lines = before.split(b"\n"), after.split(b"\n")
    if len(old_lines) != len(new_lines):
        return ()
    if any(shape(a) != shape(b) for a, b in zip(old_lines, new_lines) if a != b):
        return ()
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
        if old != new and old in SPELLINGS and new in SPELLINGS:
            if {old, new} & {"Is", "IsNot"} and not all(map(identity_operand, (a.left, a.comparators[0]))):
                return ()
            for i, (node, source) in enumerate(((a, before), (b, after))):
                span = operator_span(source, offsets[i], node, type(node.ops[0]).__name__)
                if span is None:
                    return ()
                spans[i].append(span)
            changed.append(site if OPPOSITE[old] == new else site + "\0unseen")
            b.ops[0] = a.ops[0]
        elif old == new and old in NUMERIC:
            original = (a.left, a.comparators[0])
            updated = (b.left, b.comparators[0])
            if any(sum(integer_value(v) is not None for v in pair) != 1 for pair in (original, updated)):
                continue
            for index, (x, y) in enumerate(zip(original, updated)):
                was, now = integer_value(x), integer_value(y)
                if was is None or now is None or was == now:
                    continue
                for i, v in enumerate((x, y)):
                    start = offsets[i][v.lineno - 1]
                    spans[i].append((start + v.col_offset, start + v.end_col_offset))
                changed.append(site + "\0" + str(now))
                if index == 0:
                    b.left = x
                else:
                    b.comparators[0] = x
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
        self.verified = index.get("verified", {})
        self.visits = index.get("visits", {})
        self.legacy = set(index.get("legacy", ()))
        self.legacy_identity = index.get("legacy_identity", {})

    @classmethod
    def load(cls, path, rows):
        path = Path(path)
        extended = path.with_name("relation-order-index.json.gz")
        if not path.is_file():
            path = extended
        if not path.is_file():
            return None
        with gzip.open(path, "rt") as f:
            index = json.load(f)
        if index["version"] != 1 or index["nodes"] != [r["id"] for r in rows]:
            return None
        if extended.is_file() and extended != path:
            with gzip.open(extended, "rt") as f:
                other = json.load(f)
            if other.get("version") == 1 and other.get("nodes") == index["nodes"]:
                for path_key, ids in other["reads"].items():
                    index["reads"].setdefault(path_key, []).extend(ids)
                # When both runs saw a test, an incompatible or uncertain run vetoes
                # its equality proof. Ordered/threshold predicates were observed only by the second run.
                first = set(index["observed"]) | set(index["uncertain"])
                second = set(other["observed"]) | set(other["uncertain"])
                overlap = first & second
                index["legacy"] = first
                index["legacy_identity"] = dict(index["proofs"])
                index["verified"] = other.get("verified", {})
                index["visits"] = other.get("visits", {})
                for site in index["proofs"].keys() | other["proofs"].keys():
                    old = set(index["proofs"].get(site, ()))
                    new = set(other["proofs"].get(site, ()))
                    if site.rsplit(":", 1)[-1] in {"Eq", "Is", "NotEq", "IsNot"}:
                        index["proofs"][site] = (old - second) | (new - first) | (old & new & overlap)
                    else:
                        index["proofs"][site] = new
                index["ranges"] = other.get("ranges", {})
                index["hashes"] = other.get("hashes", {})
        return cls(index)

    def proven(self, changes, candidates, *, readers=()):
        """Return measured pass IDs. Preserve any additional caller-supplied readers."""
        out = set(candidates) - set(readers)
        saw_change = False
        for change in changes:
            saw_change = True
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
                unseen = set(self.verified.get(change.path, ())) - set(self.visits.get(key, ()))
                if key.rsplit(":", 1)[-1] in {"Eq", "Is", "NotEq", "IsNot"}:
                    prior = set() if sep else set(self.legacy_identity.get(key, ()))
                    unseen.difference_update(self.legacy - prior)
                if not sep:
                    unseen.update(self.proofs.get(key, ()))
                elif value != "unseen":
                    target = int(value)
                    unseen.update(n for n, lo, hi in self.ranges.get(key, []) if lo <= target <= hi)
                out.intersection_update(unseen)
        return out if saw_change else set()
