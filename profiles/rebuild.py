"""Rebuild runtime profiles in a separate scratch directory from the base tree and test manifest."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


HERE = Path(__file__).resolve().parent
STAGES = ("shared", "isolated", "owners", "direct", "positions", "code-readers", "test-methods",
          "paths", "class-fixtures", "relations", "ordered-relations")


def source_hash(root):
    skip = {".git", ".pytest_cache", "__pycache__"}
    names = sorted(f.relative_to(root).as_posix() for f in root.rglob("*")
                   if f.is_file() and not skip.intersection(f.relative_to(root).parts) and f.suffix != ".pyc")
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b"\0" + hashlib.sha256((root / name).read_bytes()).digest())
    return digest.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stages", nargs="+", choices=STAGES)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--jobs", type=int, choices=(1,2,3,4), default=2)
    p.add_argument("--peer-observations", type=Path, help="code reader reports from the other operating system")
    args = p.parse_args()
    root = args.work.resolve()
    source = args.source.resolve()
    if root == source or root.is_relative_to(source) or source.is_relative_to(root):
        p.error("source and scratch directories must be separate")
    if root.is_relative_to(HERE.parent) or HERE.parent.is_relative_to(root):
        p.error("keep scratch outside the selector checkout")
    lab, out = root/"lab", root/"solution"
    revision = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
    if (source/".git").exists():
        rev = subprocess.check_output(["git","-C",str(source),"rev-parse","HEAD"],text=True).strip()
        if rev != revision: p.error("expected source revision "+revision)
    pinned_tree = "7719109c00f2266c252d529cd9380d0274d3dd260a06d90501fbf4e798e10893"
    if source_hash(source) != pinned_tree:
        p.error("source contents do not match the pinned archive")
    catalog = json.loads(args.manifest.read_bytes())["tests"]
    digest = hashlib.sha256("\n".join(r["id"] for r in catalog).encode()).hexdigest()
    if digest != "97a4a74dd286c8864c0093b8b09b312879a64fe9fa85e783a233206caa88aa13":
        p.error("test IDs do not match the pinned baseline")
    lab.mkdir(parents=True,exist_ok=True)
    out.mkdir(exist_ok=True)
    shutil.copyfile(HERE.parent / "isolated.py", out / "isolated.py")
    for original in HERE.rglob("*"):
        if not original.is_file() or original.suffix not in {".py",".c"} or original == Path(__file__).resolve():
            continue
        target = lab / original.relative_to(HERE)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(original,target)
    if not (lab/"source").exists():
        shutil.copytree(source,lab/"source",symlinks=True,ignore=shutil.ignore_patterns(".git","__pycache__",".pytest_cache"))
    elif source_hash(lab/"source") != pinned_tree:
        p.error("scratch source no longer matches the pinned archive")
    manifest = lab/"starter/sample/tests.json"
    manifest.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(args.manifest,manifest)
    env = dict(os.environ, SKIP_INTEGRATION="0",OPENBLAS_NUM_THREADS="1",OMP_NUM_THREADS="1",
               PYTHON_CPU_COUNT="2",BASELINE_RECORD_CHILD="1",
               PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ["PATH"])
    def run(script,*options):
        subprocess.run([sys.executable,str(lab/script),*map(str,options)],cwd=lab,env=env,check=True)
    for stage in args.stages:
        if stage == "shared":
            run("build-index.py","observe","--records",lab/".shared-lines")
            run("build-index.py","observe","--records",lab/".shared")
            run("build-index.py","index","--records",lab/".shared","--line-records",lab/".shared-lines",
                "--output",out/"coverage-index.json.gz")
        elif stage == "isolated":
            run("isolated-index.py","run","--jobs",args.jobs)
        elif stage == "owners":
            run("owner-index.py","run","--records",lab/".owners")
            run("owner-index.py","index","--records",lab/".owners","--output",out/"owner-index.json.gz")
        elif stage == "positions":
            run("position-readers.py")
        elif stage == "direct":
            run("direct-references.py")
        elif stage == "code-readers":
            peer = ["--peer-observations", args.peer_observations.resolve()] if args.peer_observations else []
            run("code-readers-run.py", *peer)
        elif stage == "test-methods":
            run("test-edits-profile.py","run",args.jobs)
        elif stage == "paths":
            run("path-observation/observe.py")
        elif stage == "class-fixtures":
            run("setup-observation/build.py")
        else:
            flags = ["--native"] if stage == "ordered-relations" else []
            if flags:
                run("relation-index.py","compile-native",*flags)
            run("relation-index.py","build",*flags)
            run("relation-index.py","run",*flags,"--threshold",0 if flags else .3,
                "--workers",min(args.jobs,2))
    print("Generated profiles:",out)


if __name__ == "__main__":
    main()
