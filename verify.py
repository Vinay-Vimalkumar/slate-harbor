"""Confirm selected tests in a fresh process running an independently patched checkout."""
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic


def _checkout(base, destination, changes, deadline):
    # The pinned checkout has roughly 17 MiB of tracked files. Materializing
    # them preserves __file__, fixture lookups and spawned Python interpreters.
    # Ignore build caches. Leave enough of the 64 MiB tmpfs for the tests.
    remaining = 28 * 1024 * 1024
    updated = set()
    paths = {}
    for name, content in changes.items():
        path = PurePosixPath(name)
        if (path.is_absolute() or not path.parts or ".." in path.parts or str(path) != name
                or any(part in {".git", "__pycache__", ".pytest_cache"} for part in path.parts)):
            raise ValueError("unsupported change path")
        if content is not None:
            remaining -= len(content)
        paths[path] = content
    if remaining < 0:
        raise ValueError("patch exceeds temporary workspace budget")
    changed_dirs = {path.parent for path in paths}

    def visit(source, target, prefix):
        nonlocal remaining
        target.mkdir()
        for entry in os.scandir(source):
            if monotonic() >= deadline:
                raise TimeoutError
            if entry.name in {".git", ".pytest_cache"}:
                continue
            rel = prefix / entry.name
            out = target / entry.name
            if entry.name == "__pycache__":
                # Reuse immutable build-time bytecode for unchanged siblings.
                # A changed module must never load a stale timestamped pyc.
                if prefix not in changed_dirs and entry.is_dir():
                    out.symlink_to(os.path.realpath(entry.path), target_is_directory=True)
                continue
            if rel in paths:
                if not entry.is_file() or entry.is_symlink():
                    raise ValueError("unsupported replacement target")
                updated.add(rel)
                if paths[rel] is not None:
                    out.write_bytes(paths[rel])
                    shutil.copymode(entry.path, out)
            elif entry.is_symlink():
                link = os.readlink(entry.path)
                if not os.path.isabs(link) and Path(entry.path).resolve().is_relative_to(base):
                    out.symlink_to(link)
                elif entry.is_file():
                    remaining -= entry.stat().st_size
                    if remaining < 0:
                        raise ValueError("checkout exceeds temporary workspace budget")
                    shutil.copy2(entry.path, out)
                else:
                    raise ValueError("unsupported base symlink")
            elif entry.is_dir():
                visit(entry.path, out, rel)
            elif entry.is_file():
                remaining -= entry.stat().st_size
                if remaining < 0:
                    raise ValueError("checkout exceeds temporary workspace budget")
                shutil.copy2(entry.path, out)
            else:
                raise ValueError("unsupported base file")

    visit(base, destination, PurePosixPath())
    for rel in paths.keys() - updated:
        out = destination / rel
        if out.resolve() != out or paths[rel] is None:
            raise ValueError("unsupported new path")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(paths[rel])


class _Reports:
    def __init__(self, nodes):
        self.phases = {node: set() for node in nodes}
        self.inconclusive = set()
        self.finished = set()

    def pytest_runtest_logreport(self, report):
        if report.nodeid not in self.phases:
            return
        # pytest 9 emits a separate report for each unittest subtest. The
        # enclosing call can pass even if one of the subtests failed.
        if not report.passed or hasattr(report, "wasxfail"):
            self.inconclusive.add(report.nodeid)
        elif not hasattr(report, "context"):
            self.phases[report.nodeid].add(report.when)

    def pytest_runtest_logfinish(self, nodeid, location):
        self.finished.add(nodeid)

    def passed(self):
        return [node for node in self.finished - self.inconclusive
                if self.phases.get(node) == {"setup", "call", "teardown"}]


def _worker(request, result):
    import pytest

    nodes = json.loads(Path(request).read_text())
    reports = _Reports(nodes)
    sys.path[0] = os.getcwd()
    status = pytest.main(["-p", "no:cacheprovider", "--capture=no", *nodes], plugins=[reports])
    if status in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        Path(result).write_text(json.dumps(reports.passed()))


def verify(changes, candidates, *, deadline, base=Path("/opt/base")):
    """Return numbers of tests observed passing all phases on the patched tree.

    changes maps POSIX source paths to fully patched bytes, or None for removals.
    candidates is a list of catalog rows. deadline is absolute monotonic time.
    Collection errors, skips, failures and timeouts never establish a pass.
    """
    if not candidates or not changes or monotonic() >= deadline:
        return set()
    try:
        by_node = {row["id"]: row["number"] for row in candidates}
        with TemporaryDirectory(prefix="confirm-") as tmp:
            root = Path(tmp).resolve()
            tree = root / "tree"
            _checkout(Path(base).resolve(), tree, changes, deadline)
            request, result = root / "request", root / "result"
            request.write_text(json.dumps(list(by_node)))
            remaining = deadline - monotonic()
            if remaining <= 0:
                return set()
            env = dict(os.environ, PYTHONPATH=str(tree), PYTHONDONTWRITEBYTECODE="1",
                       SKIP_INTEGRATION="0")
            with subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), str(request), str(result)],
                cwd=tree, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ) as child:
                try:
                    child.wait(timeout=remaining)
                finally:
                    if child.poll() is None:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        child.wait()
            if child.returncode or not result.is_file():
                return set()
            return {by_node[node] for node in json.loads(result.read_text())}
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return set()


if __name__ == "__main__":
    _worker(*sys.argv[1:])
