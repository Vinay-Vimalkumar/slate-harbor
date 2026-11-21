"""Locate executed source/line introspection sites in the pristine code and tests."""
import ast
import gzip
import json
from pathlib import Path

LAB = Path(__file__).resolve().parent
ROOT = LAB / "source"
INDEX = LAB.parent / "solution/coverage-index.json.gz"
OUTPUT = LAB.parent / "solution/position-readers.json"
NAMES = {"__code__", "co_consts", "__firstlineno__", "co_firstlineno", "co_lines", "co_positions", "co_lnotab", "co_linetable",
         "f_lineno", "tb_lineno", "getframeinfo", "getouterframes", "getinnerframes", "getsourcelines",
         "findsource", "getsource", "getcomments", "getline", "getlines", "format_tb", "extract_tb",
         "extract_stack", "format_stack", "walk_tb", "currentframe", "_getframe", "get_instructions"}

def main():
    index = json.load(gzip.open(INDEX))
    found = {}
    for name in ["sqlglot", "tests"]:
        for f in (ROOT / name).rglob("*.py"):
            rel = f.relative_to(ROOT).as_posix()
            hits = index["lines"].get(rel, {})
            try:
                tree = ast.parse(f.read_bytes())
            except SyntaxError:
                continue
            locs = sorted({v.lineno for v in ast.walk(tree)
                           if (isinstance(v, ast.Attribute) and v.attr in NAMES)
                           or (isinstance(v, ast.Name) and v.id in NAMES)})
            if locs:
                found[rel] = {line:hits.get(str(line), []) for line in locs}
    OUTPUT.write_text(json.dumps(found,separators=(",",":")))
    print(f"{len(found)} modules, {sum(map(len,found.values()))} sites")

if __name__ == "__main__":
    main()
