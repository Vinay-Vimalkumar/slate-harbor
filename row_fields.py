"""Infer which tuple columns a class method reads from a stored list of rows."""
import ast
from functools import lru_cache

ALL = frozenset(range(3))


@lru_cache(maxsize=16)
def columns(source, class_name, method_name, field):
    tree = ast.parse(source)
    klass = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    if klass is None:
        return ALL
    methods = {n.name: n for n in klass.body if isinstance(n, ast.FunctionDef)}
    active = set()
    memo = {}

    def visit(name, parameter=None):
        key = (name, parameter)
        if key in memo:
            return memo[key]
        if key in active or name not in methods:
            return ALL
        active.add(key)
        node = methods[name]
        args = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
        receiver = args[0] if args else None
        nodes = list(ast.walk(node))
        parents = {child: parent for parent in nodes for child in ast.iter_child_nodes(parent)}
        loads = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        result = set()

        def unpack(target):
            if (not isinstance(target, (ast.Tuple, ast.List)) or len(target.elts) != 3
                    or any(not isinstance(x, ast.Name) for x in target.elts)):
                return ALL
            return {i for i, dest in enumerate(target.elts) if dest.id in loads}

        def used_by(value):
            parent = parents.get(value)
            if isinstance(parent, ast.Subscript) and parent.value is value:
                item = parent
                binding = parents.get(item)
                while isinstance(binding, ast.IfExp) and item in (binding.body, binding.orelse):
                    item, binding = binding, parents.get(binding)
                if isinstance(binding, ast.Assign) and len(binding.targets) == 1:
                    return unpack(binding.targets[0])
                return ALL
            if isinstance(parent, (ast.For, ast.comprehension)) and parent.iter is value:
                return unpack(parent.target)
            if not isinstance(parent, ast.Call) or value not in parent.args:
                return ALL
            func = parent.func
            if isinstance(func, ast.Name) and func.id == "enumerate" and parent.args[0] is value:
                loop = parents.get(parent)
                if (isinstance(loop, (ast.For, ast.comprehension)) and loop.iter is parent
                        and isinstance(loop.target,(ast.Tuple,ast.List)) and len(loop.target.elts)==2):
                    return unpack(loop.target.elts[1])
                return ALL
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id == receiver and func.attr in methods):
                callee = methods[func.attr].args
                arguments = [a.arg for a in (*callee.posonlyargs, *callee.args)]
                pos = parent.args.index(value) + 1
                if pos < len(arguments) and not any(isinstance(a,ast.Starred) for a in parent.args):
                    return visit(func.attr, arguments[pos])
            return ALL

        for n in nodes:
            if isinstance(n, ast.Name) and n.id in {"locals","vars","eval","exec","getattr","setattr","delattr"}:
                result.update(ALL)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == receiver:
                parent = parents.get(n)
                if not isinstance(parent, ast.Attribute) or parent.attr.startswith("__"):
                    result.update(ALL)
            elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == receiver:
                if n.attr == field:
                    result.update(used_by(n))
                elif n.attr in methods:
                    parent = parents.get(n)
                    if isinstance(parent, ast.Call) and parent.func is n:
                        result.update(visit(n.attr))
                    else:
                        result.update(ALL)
            elif isinstance(n, ast.Name) and n.id == parameter and isinstance(n.ctx, ast.Load):
                result.update(used_by(n))
            if result == ALL:
                break
        active.remove(key)
        memo[key] = frozenset(result)
        return memo[key]

    result = visit(method_name)
    return result or ALL
