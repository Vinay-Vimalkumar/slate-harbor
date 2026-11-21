"""Narrow a blanket observer only if all of its reached local reflection sites admit precise attribution."""
import ast
import gzip
import hashlib
import json
from pathlib import Path
import sys

LAB = Path(__file__).resolve().parent
SOL = LAB.parent / "solution"
sys.path.insert(0,str(SOL))
from isolated import Isolated

FIELDS = {"co_code","co_names","co_varnames","co_consts","co_flags","co_argcount","co_kwonlyargcount",
          "co_posonlyargcount","co_nlocals","co_stacksize","co_freevars","co_cellvars","co_name",
          "co_qualname","co_filename","co_firstlineno","co_linetable","co_lnotab","co_exceptiontable"}


def dotted(node):
    return isinstance(node,ast.Name) or (isinstance(node,ast.Attribute) and dotted(node.value))


def code_lookup(node):
    return isinstance(node,ast.Attribute) and node.attr=="__code__" and dotted(node.value)


def pair_assignments(fn):
    # Local code handles must be obtained directly from a named callable and have
    # exactly one assignment in the scope. Unrelated values cannot flow into the handle.
    stores={}
    origins={}
    for n in ast.walk(fn):
        if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Store):
            stores[n.id]=stores.get(n.id,0)+1
        if not isinstance(n,ast.Assign) or len(n.targets)!=1:
            continue
        left,right=n.targets[0],n.value
        pairs=(zip(left.elts,right.elts) if isinstance(left,ast.Tuple) and isinstance(right,ast.Tuple)
               and len(left.elts)==len(right.elts) else [(left,right)])
        for name,value in pairs:
            if (isinstance(name,ast.Name) and isinstance(value,ast.Attribute) and value.attr=="__code__"
                    and dotted(value.value)):
                origins[name.id]=True
    return {name for name in origins if stores.get(name)==1}


def attributable(tree, line):
    functions=sorted((n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
                      and n.lineno<=line<=n.end_lineno),key=lambda n:n.end_lineno-n.lineno)
    if not functions:
        return False
    fn=functions[0]
    known=pair_assignments(fn)
    found=False
    for node in ast.walk(fn):
        if getattr(node,"lineno",None)!=line:continue
        if isinstance(node,ast.Attribute):
            if code_lookup(node):
                found=True
            elif node.attr in FIELDS:
                if not code_lookup(node.value) and not (isinstance(node.value,ast.Name) and node.value.id in known):return False
                found=True
            elif node.attr in {"f_lineno","tb_lineno","getframeinfo","getouterframes","getinnerframes",
                                "getsourcelines","findsource","getsource","getcomments","getline","getlines",
                                "format_tb","extract_tb","extract_stack","format_stack","walk_tb","currentframe",
                                "_getframe","get_instructions","co_positions","co_lines","__firstlineno__"}:
                return False
    return found


def candidate_sites(rows):
    iso=Isolated.load(SOL/"isolated-index.gz",rows)
    if iso is None:
        raise RuntimeError("matching isolated-index.gz is required")
    positions=json.loads((SOL/"position-readers.json").read_bytes())
    by_test={}
    for file, entries in positions.items():
        for line,nums in entries.items():
            for n in set(nums)|iso.at(file,int(line)):
                by_test.setdefault(n,[]).append((file,int(line)))
    return by_test


def pack():
    rows=json.loads((LAB/"starter/sample/tests.json").read_bytes())["tests"]
    pristine=LAB/"source"
    by_test=candidate_sites(rows)
    index={"version":1,"revision":"eb11aec4be8cb2122afe713a4816662c3f0fb2fa","catalog":hashlib.sha256("\n".join(r["id"] for r in rows).encode()).hexdigest(),
           "reviewed":[],"code":{},"raw":{}}
    saved={}
    for n, locs in by_test.items():
        native,linux=LAB/f"code-readers-native-{n}.json", LAB/"code-readers-linux"/f"{n}.json"
        if not native.is_file() or not linux.is_file():continue
        reports=[json.loads(p.read_bytes()) for p in (native,linux)]
        if [r.get("platform") for r in reports] != ["darwin","linux"]:continue
        if any(r["node"]!=rows[n]["id"] or r["exit"] or r["subprocess"] or r["unknown"] or r["opaque"] for r in reports):
            continue
        if not all(attributable(ast.parse((pristine/p).read_bytes()),line) for p,line in locs):
            continue
        # Some __code__ lookups occur while testing for attribute presence. Require
        # at least one actual audit event attributable to the project's own reader.
        if not all(any(e[0] in {p for p,_ in locs} and e[2]=="__code__" for e in r["origins"]) for r in reports):
            continue
        index["reviewed"].append(n)
        for kind in ("code","raw"):
            paths=set().union(*(r[kind] for r in reports))
            for p in sorted(paths):
                index[kind].setdefault(p,[]).append(n)
        saved[n]=locs
    target=SOL/"code-readers.json.gz"
    tmp=target.with_suffix(".tmp");tmp.write_bytes(gzip.compress(json.dumps(index,separators=(",",":")).encode(),mtime=0))
    tmp.replace(target)
    print("reviewed",saved,"code paths",len(index["code"]),"raw paths",len(index["raw"]),"bytes",target.stat().st_size)


if __name__=="__main__":pack()
