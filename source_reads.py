"""Find the evaluated portion of a compound header or short-circuit expression."""
import ast
from collections import defaultdict
from functools import lru_cache


@lru_cache(maxsize=32)
def _headers(source):
    by_line = defaultdict(set)

    def link(first, last, *also):
        bounds = ((first, last), *also)
        if first == last and not also:
            return
        for line in range(first, last + 1):
            by_line[line].update(bounds)

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.If, ast.While)):
            link(node.lineno, node.test.end_lineno)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            link(node.lineno, node.iter.end_lineno)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            final = node.items[-1]
            link(node.lineno, (final.optional_vars or final.context_expr).end_lineno)
        elif isinstance(node, ast.Assert):
            link(node.lineno, (node.msg or node.test).end_lineno)
        elif isinstance(node, (ast.BoolOp, ast.IfExp, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            link(node.lineno, node.end_lineno)
        elif isinstance(node, ast.Compare) and len(node.ops) > 1:
            link(node.lineno, node.end_lineno)
        elif isinstance(node, ast.Match):
            subject = (node.lineno, node.subject.end_lineno)
            link(*subject)
            for branch in node.cases:
                link(branch.pattern.lineno, (branch.guard or branch.pattern).end_lineno, subject)
        elif isinstance(node, (ast.Try, ast.TryStar)):
            for branch in node.handlers:
                end = branch.type.end_lineno if branch.type is not None else branch.lineno
                link(branch.lineno, end, (node.lineno, node.lineno))

    # Nested expressions frequently contribute the same observations. Merge their
    # overlapping intervals without bridging across a match or exception suite.
    merged = {}
    for line, intervals in by_line.items():
        spans = []
        for lo, hi in sorted(intervals):
            if spans and lo <= spans[-1][1] + 1:
                spans[-1] = spans[-1][0], max(hi, spans[-1][1])
            else:
                spans.append((lo, hi))
        merged[line] = tuple(spans)
    return merged


def evaluation_spans(source: bytes, line: int):
    """Return inclusive base-line ranges that may precede evaluation of this line."""
    return _headers(source).get(line, ())
