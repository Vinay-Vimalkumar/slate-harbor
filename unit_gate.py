"""Low-overhead eligibility rules for optional test replay."""
from functools import lru_cache
from pathlib import Path
import re


@lru_cache(maxsize=128)
def _imports_core(base, file):
    source = (base / file).read_bytes()
    return bool(re.search(rb"(?m)^(?:import sqlglot\b|from sqlglot[.\s])", source))


def should_verify(changes, rows, *, base=Path("/opt/base")):
    """Identify small, leaf-dialect groups where clearing one worker can cover replay."""
    if not changes or not 0 < len(rows) <= 20 or sum(r["seconds"] for r in rows) > 0.5:
        return False
    if not all(n.startswith(("sqlglot/parsers/", "sqlglot/generators/"))
               and n.endswith(".py") and not n.endswith("/__init__.py")
               and "/" not in n.split("/", 2)[2] for n in changes):
        return False
    for row in rows:
        file = row["id"].split("::", 1)[0]
        if (not file.startswith("tests/") or not file.endswith(".py")
                or ".." in Path(file).parts
                or file in {"tests/test_executor.py", "tests/test_optimizer.py"}):
            return False
        try:
            if not file.startswith("tests/dialects/") and not _imports_core(base, file):
                return False
        except OSError:
            return False
    return True
