"""Verify candidate tests in disposable children of a dependency-warmed interpreter."""
import importlib
import os
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from verify import _checkout, _Reports


def _can_fork():
    # Native libraries can have threads not returned by threading.enumerate().
    # Never fork a process that currently holds another thread's locks.
    return sys.platform.startswith("linux") and len(os.listdir("/proc/self/task")) == 1


def _prepare(candidates):
    if not _can_fork():
        return False
    importlib.import_module("pytest")
    importlib.import_module("unittest.mock")
    # These pinned test modules use pandas for dataframe comparisons. Loading it
    # once is safe only in a single-threaded parent with no source-tree imports.
    if any(row["id"].split("::")[0] in {"tests/test_optimizer.py", "tests/test_executor.py"}
           for row in candidates):
        importlib.import_module("pandas")
    return _can_fork() and not any(name.split(".", 1)[0] in {"sqlglot", "tests"}
                                   for name in sys.modules)


def _run_tests(tree, rows, output):
    import pytest

    os.chdir(tree)
    os.environ.update(PYTHONPATH=str(tree), PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0")
    sys.path.insert(0, str(tree))
    # Spawned workers import the functions in their true test modules. An
    # application that embedded us may have its own non-importable entry script.
    main = sys.modules.get("__main__")
    if main is not None:
        main.__spec__ = None
        main.__file__ = None
    sys.argv = ["pytest"]
    node_ids = {row["id"]: row["number"] for row in rows}
    reports = _Reports(node_ids)
    status = pytest.main(["--capture=no", "-p", "no:cacheprovider", *node_ids], plugins=[reports])
    if status in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        output.write_text("\n".join(str(node_ids[node]) for node in reports.passed()))


def _one_group(changes, rows, deadline, base):
    with TemporaryDirectory(prefix="fv-") as temporary:
        directory = Path(temporary).resolve()
        tree = directory / "src"
        _checkout(base, tree, changes, deadline)
        if monotonic() >= deadline:
            return set()
        output = directory / "passed"
        pid = os.fork()
        if pid == 0:
            try:
                os.setsid()
                with open(os.devnull, "r+b", buffering=0) as null:
                    for fd in (0, 1, 2):
                        os.dup2(null.fileno(), fd)
                _run_tests(tree, rows, output)
            except BaseException:
                os._exit(1)
            os._exit(0)

        status = None
        try:
            while monotonic() < deadline:
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                status = None
                sleep(min(0.008, max(0, deadline - monotonic())))
        finally:
            # The budget applies to descendants, including spawned Python pools.
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
        if status is None or os.waitstatus_to_exitcode(status) != 0 or not output.is_file():
            return set()
        return {int(line) for line in output.read_text().splitlines()}


def verify(changes, candidates, *, deadline, base=Path("/opt/base")):
    """Return IDs of complete passes, under an absolute monotonic wall deadline."""
    if not candidates or not changes or monotonic() >= deadline:
        return set()
    confirmed = set()
    try:
        if not _prepare(candidates):
            return confirmed
        # Class and module fixtures can carry state between tests. Keep the
        # caller's candidate order in one pytest session on this patched tree.
        if monotonic() < deadline:
            confirmed.update(_one_group(changes, candidates, deadline, Path(base).resolve()))
    except (OSError, ValueError, ImportError, KeyError, TimeoutError):
        pass
    return confirmed
