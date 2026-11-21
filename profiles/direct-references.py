"""Resolve explicit test references to class objects against observed source lines."""
import ast
from collections import defaultdict
import gzip
import hashlib
import json
import marshal
from pathlib import Path

LAB = Path(__file__).resolve().parent
OUT = LAB.parent/"solution"


def main():
    rows = json.loads((LAB/"starter/sample/tests.json").read_bytes())["tests"]
    shared = json.loads(gzip.decompress((OUT/"coverage-index.json.gz").read_bytes()))
    owner = json.loads(gzip.decompress((OUT/"owner-index.json.gz").read_bytes()))
    cold = marshal.loads(gzip.decompress((OUT/"isolated-index.gz").read_bytes()))
    if [r["id"] for r in rows] != cold["nodes"] or cold["missing"] or cold["uncertain"]:
        raise ValueError("explicit references need a fully observed baseline")
    by_file = defaultdict(set)
    for r in rows:
        by_file[r["file"]].add(r["number"])
    names = set(owner["direct_tests"])
    result = defaultdict(set)
    def add_mask(dest,mask):
        while mask:
            bit=mask&-mask
            dest.add(bit.bit_length()-1)
            mask^=bit
    for path, group in by_file.items():
        module = ast.parse((LAB/"source"/path).read_bytes())
        aliases = {a.asname:a.name for n in ast.walk(module) if isinstance(n,ast.ImportFrom) for a in n.names if a.asname}
        points=defaultdict(set)
        broad=set()
        def scan(n, in_function=False):
            if isinstance(n,ast.Name):
                candidates=[n.id,aliases.get(n.id,"")]
            elif isinstance(n,ast.Attribute):
                candidates=[n.attr]
            else:
                candidates=[]
            for name in candidates:
                if name in names:
                    if in_function:
                        points[name].add(n.lineno)
                    else:
                        broad.add(name)
            if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.Lambda)):
                body=n.body if isinstance(n.body,list) else [n.body]
                for sub in ast.iter_child_nodes(n):
                    scan(sub,in_function or sub in body)
            else:
                for sub in ast.iter_child_nodes(n):
                    scan(sub,in_function)
        scan(module)
        for name in names:
            if not group.intersection(owner["direct_tests"][name]):
                continue
            if name in broad or name not in points:
                result[name].update(group)
                continue
            for line in points[name]:
                masked=cold["masks"][cold["lines"].get(path,{}).get(line,0)]
                ids=shared["lines"].get(path,{}).get(str(line),())
                if not ids and not masked:
                    # Missing line attribution, including continuation lines.
                    result[name].update(group)
                else:
                    result[name].update(ids)
                    add_mask(result[name],masked)
    index={"nodes":[r["id"] for r in rows],"direct":{k:sorted(v) for k,v in result.items()},
           "owner_sha":hashlib.sha256((OUT/"owner-index.json.gz").read_bytes()).hexdigest()}
    dest=OUT/"direct-index.json.gz"
    with dest.open("wb") as f:
        with gzip.GzipFile(filename="",fileobj=f,mode="wb",mtime=0) as z:
            z.write(json.dumps(index,separators=(",",":")).encode())
    print(f"{len(result)} class names; {sum(map(len,owner['direct_tests'].values()))} old test references; {sum(map(len,result.values()))} refined test references")


if __name__=="__main__":
    main()
