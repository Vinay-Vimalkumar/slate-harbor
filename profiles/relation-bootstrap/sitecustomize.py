"""Recorder for a private, instrumented copy of the pinned test environment."""
from _thread import get_ident
from importlib.util import source_from_cache
import atexit
import builtins
import json
import operator
import re
import os
import sys
import types

OPS = (operator.is_, operator.is_not, operator.eq, operator.ne)
PREFIX = os.environ["REL_SOURCE"] + os.sep
DEST = os.path.join(os.environ["REL_OUT"], str(os.getpid()))
open(DEST + ".start", "wb").close()
good, bad, reads = set(), set(), set()
verified_modules, unverified_modules = set(), set()
unknown = False
reasons = set()
native = None
if os.environ.get("REL_NATIVE"):
    import _relation_native as native
    unknown = bool(sys.flags.bytes_warning)
safe_immutable = frozenset(id(c) for c in (type(None), bool, int, float, str, bytes))


def descriptors(cls):
    # Read dictionary entries without invoking user descriptors or metaclass lookup.
    for name in ("__eq__", "__ne__"):
        for base in type.__getattribute__(cls, "__mro__"):
            namespace = type.__getattribute__(base, "__dict__")
            if name in namespace:
                yield namespace[name]
                break


pairs = tuple((c.__eq__, c.__ne__) for c in (object, str, bytes, int, float))


def standard(cls):
    if id(cls) in safe_immutable:
        return True
    eq, ne = descriptors(cls)
    return any(eq is a and ne is b for a, b in pairs)


def observe(site, op, left, right):
    raw = OPS[op]
    if site in bad:
        return raw(left, right)
    # Record uncertainty before the original operation: the test may catch an exception.
    if not standard(type(left)) or not standard(type(right)):
        bad.add(site)
        return raw(left, right)
    original = raw(left, right)
    alternate = OPS[(op + 2) % 4](left, right)
    if type(original) is bool and original is alternate:
        good.add(site)
    else:
        bad.add(site)
    return original


audit_walk = set()


def audit(event, args):
    global unknown
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty", "os.exec"}:
        unknown = True
        reasons.add(event)
    elif event in {"exec", "function.__new__"} and native:
        code = args[0]
        name = code.co_filename
        if name.startswith(PREFIX + "sqlglot/"):
            path = name[len(PREFIX):]
            marker = ("\0rwx-rel-observed-v1/" + path).encode()
            if code.co_name == "<module>" and code.co_consts and code.co_consts[-1] == marker:
                verified_modules.add(path)
            else:
                unverified_modules.add(path)
        elif os.path.isabs(name) and "/sqlglot/" in name:
            # SQLGlot was loaded from another location in this process.
            unverified_modules.add("sqlglot/" + name.rsplit("/sqlglot/", 1)[1])
    elif event == "open":
        file = args[0]
        if isinstance(file, (bytes, str)):
            path = os.path.abspath(os.fsdecode(file))
            if path.startswith(PREFIX) and path.endswith((".py", ".pyc")):
                caller = sys._getframe(1).f_code
                if not caller.co_filename.startswith("<frozen importlib."):
                    if path.endswith(".pyc"):
                        try:
                            path = source_from_cache(path)
                        except ValueError:
                            directory, cache = os.path.split(path)
                            match = re.fullmatch(r"([A-Za-z_]\w*)\.cpython-\d+-pytest-[0-9.]+\.pyc", cache)
                            if match and os.path.basename(directory) == "__pycache__":
                                path = os.path.join(os.path.dirname(directory), match.group(1) + ".py")
                            else:
                                unknown = True
                                reasons.add("unresolved bytecode read " + path[len(PREFIX):])
                    reads.add(path[len(PREFIX):])
    # Source or code inspection by the test/program invalidates this observation.
    elif event == "object.__getattr__" and args[1] in {"__code__", "co_code", "f_code", "tb_frame"}:
        thread = get_ident()
        if thread in audit_walk:
            return
        audit_walk.add(thread)
        try:
            frame = sys._getframe(1)
            while frame is not None:
                code = frame.f_code
                file = code.co_filename
                module = frame.f_globals.get("__name__", "")
                # CPython 3.13 _signature_from_function reads argument counts, names
                # and flags from __code__. These comparison edits preserve all three.
                if args[1] == "__code__" and module == "inspect" and code.co_name == "_signature_from_function":
                    break
                if file.startswith(PREFIX):
                    target = args[0]
                    kind = type(target)
                    if kind is types.FunctionType:
                        target = target.__code__
                    elif kind is types.FrameType:
                        target = target.f_code
                    elif kind is types.TracebackType:
                        target = target.tb_frame.f_code
                    if type(target) is types.CodeType and os.path.isabs(target.co_filename):
                        name = os.path.abspath(target.co_filename)
                        if name.startswith(PREFIX):
                            reads.add(name[len(PREFIX):])
                    else:
                        unknown = True
                        reason = f"{args[1]} read in {file[len(PREFIX):]}:{frame.f_lineno}"
                        reasons.add(reason)
                    break
                if module not in {"inspect", "dis", "linecache", "traceback", "tokenize"}:
                    break
                frame = frame.f_back
        finally:
            audit_walk.discard(thread)


def finish():
    if native:
        eligible, ineligible, intervals, invalid = native.snapshot()
        data = [eligible, ineligible, sorted(reads), unknown, intervals, invalid, sorted(reasons), sorted(verified_modules), sorted(unverified_modules)]
    else:
        data = [sorted(good), sorted(bad), sorted(reads), unknown]
    with open(DEST + ".pending", "w") as out:
        json.dump(data, out)
    os.replace(DEST + ".pending", DEST + ".json")
    if os.getenv("REL_REASONS") and reasons:
        print("Relation uncertainty", sorted(reasons), file=sys.stderr)


builtins._rwx_operand_check_70a = native.observe if native else observe
atexit.register(finish)
sys.addaudithook(audit)
