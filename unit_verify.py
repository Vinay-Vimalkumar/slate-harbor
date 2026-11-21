"""Replay compatible unittest cases in their original order on a patched source tree."""
import importlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import sys
from multiprocessing.connection import Pipe
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
import unittest.mock  # Shared by many cases and safe to preload before a fork.

from unit_gate import should_verify
from verify import _checkout


_cache = None


class Workspace:
    def __init__(self, base, deadline):
        self.base = base
        self.warm = None
        self.warm_attempted = False
        self.tmp = TemporaryDirectory(prefix="uv-replay-")
        self.tree = Path(self.tmp.name) / "src"
        _checkout(base, self.tree, {}, deadline)
        # The scorer's base is read-only. Preserve real paths for __file__ and
        # fixture lookups. Only build-time pyc directories point into the base.
        for root, dirs, files in os.walk(self.tree, topdown=False):
            for file in files:
                path = Path(root, file)
                if not path.is_symlink():
                    path.chmod(0o555 if os.access(path, os.X_OK) else 0o444)
            os.chmod(root, 0o555)

    def stamp(self):
        # An unexpected write invalidates every observation and the cached tree.
        state = []
        for root, dirs, files in os.walk(self.tree):
            for n in sorted([*dirs, *files]):
                s = os.lstat(os.path.join(root, n))
                state.append((root, n, s.st_mode, s.st_size, s.st_ctime_ns))
        return state

    def close(self):
        if self.warm is not None:
            self.warm.close()
        for root, dirs, files in os.walk(self.tree):
            os.chmod(root, 0o755)
        self.tmp.cleanup()

    def install(self, changes):
        cache_roots = set()
        for name, contents in changes.items():
            path = self.tree / name
            path.chmod(0o644)
            path.write_bytes(contents)
            path.chmod(0o444)
            if path.suffix == ".py":
                cache_roots.add(path.parent)
        # Imports must recompile edited sources even with identical timestamps,
        # byte lengths, or an unchecked hash header in the original pyc.
        for directory in cache_roots:
            cache = directory / "__pycache__"
            if not cache.is_symlink():
                continue
            original = cache.resolve()
            directory.chmod(0o755)
            cache.unlink()
            cache.mkdir()
            edited = {Path(n).stem for n in changes if (self.tree / n).parent == directory}
            for pyc in original.iterdir():
                if pyc.name.partition(".")[0] not in edited:
                    (cache / pyc.name).symlink_to(pyc)
            cache.chmod(0o555)
            directory.chmod(0o555)
        return cache_roots

    def restore(self, changes, cache_roots):
        for name in changes:
            path = self.tree / name
            path.chmod(0o644)
            shutil.copyfile(self.base / name, path)
            shutil.copystat(self.base / name, path)
            path.chmod(0o555 if os.access(self.base / name, os.X_OK) else 0o444)
        for directory in cache_roots:
            cache = directory / "__pycache__"
            pristine = self.base / directory.relative_to(self.tree) / "__pycache__"
            if cache.is_dir() and not cache.is_symlink() and pristine.is_dir():
                directory.chmod(0o755)
                cache.chmod(0o755)
                shutil.rmtree(cache)
                cache.symlink_to(pristine, target_is_directory=True)
                directory.chmod(0o555)


def _suitable(changes, base):
    for name, data in changes.items():
        p = PurePosixPath(name)
        target = base / name
        if (not p.parts or p.parts[0] not in ("sqlglot", "tests") or p.as_posix() != name
                or ".." in p.parts or p.name == "conftest.py" or "__pycache__" in p.parts
                or data is None or len(data) > 4 * 1024 * 1024
                or not target.is_file() or target.resolve() != target):
            return False
        if p.suffix == ".py":
            code = data.lower()
            if any(token in code for token in (b"pytest", b"sys.argv", b"sys.modules")):
                return False
    return True


def _pytest_hooks(namespace):
    names = ("pytestmark", "pytest_plugins", "setup_module", "teardown_module",
             "setup_class", "teardown_class", "setup_method", "teardown_method",
             "setup_function", "teardown_function")
    if namespace.get("__test__") is False or any(n in namespace for n in names):
        return True
    return any(type(obj).__module__ == "_pytest.fixtures" for obj in namespace.values())


class Results(unittest.TestResult):
    def __init__(self, identities):
        super().__init__()
        self.identities = identities
        self.passed = set()
        self.invalid = set()
        self.fixture_error = False

    def addSuccess(self, test):
        self.passed.add(self.identities[test])

    def addError(self, test, err):
        if test not in self.identities:
            self.fixture_error = True
        else:
            self.invalid.add(self.identities[test])

    def addFailure(self, test, err):
        self.invalid.add(self.identities[test])

    def addSkip(self, test, reason):
        if test not in self.identities:
            self.fixture_error = True
        else:
            self.invalid.add(self.identities[test])

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.invalid.add(self.identities[test])

    def addExpectedFailure(self, test, err):
        self.invalid.add(self.identities[test])

    def addUnexpectedSuccess(self, test):
        self.invalid.add(self.identities[test])


def _context(tree):
    os.chdir(tree)
    os.environ.update(PYTHONPATH=str(tree), PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0")
    sys.path.insert(0, str(tree))
    sys.dont_write_bytecode = True
    main = sys.modules.get("__main__")
    if main is not None:
        main.__file__ = main.__spec__ = None
    sys.argv = ["pytest"]


def _run(tree, rows):
    _context(tree)
    cases, ids = [], {}
    for row in rows:
        parts = row["id"].split("::")
        if len(parts) != 3 or not parts[0].startswith("tests/") or not parts[0].endswith(".py"):
            return set()
        file_name, class_name, method = parts
        if not (method.isidentifier() and class_name.isidentifier()):
            return set()
        module = importlib.import_module(file_name[:-3].replace("/", "."))
        if Path(module.__file__).resolve() != tree/file_name or _pytest_hooks(vars(module)):
            return set()
        cls = getattr(module, class_name)
        if not issubclass(cls, unittest.TestCase) or any(_pytest_hooks(vars(c)) for c in cls.__mro__):
            return set()
        fn = getattr(cls, method)
        if (getattr(fn, "pytestmark", ()) or getattr(fn, "__test__", True) is False
                or not callable(fn)):
            return set()
        item = cls(method)
        cases.append(item)
        ids[item] = row["number"]
    result = Results(ids)
    unittest.TestSuite(cases).run(result)
    return set() if result.fixture_error else result.passed - result.invalid


def _silence():
    with open(os.devnull, "r+b", buffering=0) as null:
        for fd in (0, 1, 2):
            os.dup2(null.fileno(), fd)


def _child(tree, rows, deadline, out):
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            _silence()
            out.write_text("\n".join(map(str, sorted(_run(tree, rows)))))
        except BaseException:
            os._exit(1)
        os._exit(0)
    status = None
    try:
        while monotonic() < deadline:
            done, code = os.waitpid(pid, os.WNOHANG)
            if done:
                status = code
                break
            sleep(0.003)
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if status is None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
    if status != 0:
        return set()
    return {int(i) for i in out.read_text().splitlines()}


def _warm_loop(connection, tree, preload):
    _silence()
    _context(tree)
    reads = set()
    watching = True

    def record(event, args):
        if watching and event == "open" and isinstance(args[0], (bytes, str)):
            reads.add(os.path.abspath(os.fsdecode(args[0])))

    sys.addaudithook(record)
    importlib.import_module(preload)
    # Record source origins too. CPython may have opened only the compiled cache.
    watching = False
    observed = set(reads)
    for path in reads:
        if path.endswith(".pyc"):
            try:
                observed.add(importlib.util.source_from_cache(path))
            except ValueError:
                pass
    for module in sys.modules.copy().values():
        path = vars(module).get("__file__") if module is not None else None
        if isinstance(path, str):
            observed.add(os.path.abspath(path))
    if len(os.listdir("/proc/self/task")) != 1:
        raise ValueError("threaded dependency import")
    connection.send_bytes(json.dumps(sorted(observed)).encode())
    while True:
        try:
            payload = connection.recv_bytes()
        except EOFError:
            return
        rows, deadline, out = json.loads(payload)
        result = _child(tree, rows, deadline, Path(out))
        connection.send_bytes(json.dumps(sorted(result)).encode())


class DialectParent:
    def __init__(self, tree, deadline, preload="sqlglot"):
        self.connection, child = Pipe()
        self.pid = os.fork()
        if self.pid == 0:
            self.connection.close()
            try:
                _warm_loop(child, tree, preload)
            except BaseException:
                os._exit(1)
            os._exit(0)
        child.close()
        try:
            if not self.connection.poll(max(0, deadline - monotonic())):
                raise TimeoutError
            self.reads = set(json.loads(self.connection.recv_bytes()))
        except BaseException:
            self.close()
            raise

    def close(self):
        self.connection.close()
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(self.pid, 0)

    def eligible(self, changes, tree, base):
        return all(str(tree / name) not in self.reads and str(base / name) not in self.reads
                   for name in changes)

    def run(self, rows, deadline, out):
        self.connection.send_bytes(json.dumps([rows, deadline, str(out)]).encode())
        # The broker owns the child and must reap it before the tree is restored.
        return set(json.loads(self.connection.recv_bytes()))


def verify(changes, candidates, *, deadline, base=Path("/opt/base")):
    """Return verified passes. Incompatible pytest cases and ambiguous errors stay scheduled."""
    global _cache
    if not (changes and candidates and monotonic() < deadline and sys.platform.startswith("linux")):
        return set()
    base = Path(base).resolve()
    try:
        if not _suitable(changes, base) or len(os.listdir("/proc/self/task")) != 1:
            return set()
        if any(n.split(".", 1)[0] in {"sqlglot", "tests"} for n in sys.modules):
            return set()
        if _cache is None or _cache.base != base:
            if _cache is not None:
                _cache.close()
                _cache = None
            if any(base.rglob("conftest.py")):
                return set()
            _cache = Workspace(base, deadline)
        dialect = should_verify(changes, candidates, base=base)
        if dialect and not _cache.warm_attempted:
            _cache.warm_attempted = True
            try:
                _cache.warm = DialectParent(_cache.tree, deadline)
            except (TimeoutError, OSError, ValueError, EOFError):
                pass
        warm = _cache.warm if dialect else None
        roots = _cache.install(changes)
        snapshot = _cache.stamp()
        out = Path(_cache.tmp.name) / "result"
        out.unlink(missing_ok=True)
        if warm is not None and warm.eligible(changes, _cache.tree, base):
            passes = warm.run(candidates, deadline, out)
        else:
            passes = _child(_cache.tree, candidates, deadline, out)
        if snapshot != _cache.stamp():
            raise ValueError("unexpected source mutation")
        _cache.restore(changes, roots)
        return passes
    except (OSError, ValueError, UnicodeError, TimeoutError, KeyError, EOFError):
        if _cache is not None:
            _cache.close()
            _cache = None
        return set()
