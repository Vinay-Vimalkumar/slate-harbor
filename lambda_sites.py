"""Locate a changed call argument that has its own constant-load instruction."""
import ast
import dis
from functools import lru_cache
from types import CodeType


@lru_cache(maxsize=8)
def compiled_lambdas(path, source):
    found = {}
    pending = [compile(source, path, "exec", dont_inherit=True)]
    while pending:
        code = pending.pop()
        pending.extend(v for v in code.co_consts if isinstance(v, CodeType))
        if code.co_name == "<lambda>":
            found.setdefault(code.co_firstlineno, []).append(code)
    return found


def literal_arguments(path, source, old, new):
    """Return required baseline execution lines, or None for an unproven edit."""
    code = compiled_lambdas(path, source).get(old.lineno, ())
    if len(code) != 1:
        return None
    points = []
    def equivalent(a, b, literal_arg=False):
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.Constant):
            if type(a.value) is not type(b.value):
                return False
            if a.value == b.value:
                return True
            if not literal_arg or type(a.value) not in (int, str, bool, bytes):
                return False
            points.append((a.lineno, a.end_lineno, a.col_offset, a.end_col_offset))
            return True
        if isinstance(a, ast.Call):
            if not equivalent(a.func,b.func) or len(a.args)!=len(b.args) or len(a.keywords)!=len(b.keywords):
                return False
            return (all(equivalent(x,y,True) for x,y in zip(a.args,b.args))
                    and all(x.arg==y.arg and equivalent(x.value,y.value,True)
                            for x,y in zip(a.keywords,b.keywords)))
        if isinstance(a, ast.AST):
            return all(equivalent(getattr(a,k),getattr(b,k)) for k in a._fields)
        if isinstance(a,list):
            return len(a)==len(b) and all(equivalent(x,y) for x,y in zip(a,b))
        return a == b
    if not equivalent(old.body, new.body) or not points:
        return None
    loads = {tuple(ins.positions) for ins in dis.get_instructions(code[0]) if ins.opname == "LOAD_CONST"}
    if not all(p in loads for p in points):
        return None
    return {p[0] for p in points}
