"""Collect physical code-reader observations locally, import opposite-OS reports, and pack complete pairs."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

LAB = Path(__file__).resolve().parent


def destination(number, system):
    if system == "linux":
        return LAB / "code-readers-linux" / f"{number}.json"
    return LAB / f"code-readers-native-{number}.json"


def peer_file(location, number, opposite):
    if location.is_file():
        return location
    if opposite == "linux":
        paths = (location / "code-readers-linux" / f"{number}.json", location / f"{number}.json")
    else:
        paths = (location / f"code-readers-native-{number}.json", location / f"{number}.json")
    return next((p for p in paths if p.is_file()), None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--peer-observations", type=Path, help="opposite-OS report file or report directory")
    args = parser.parse_args()
    if sys.platform not in {"linux", "darwin"}:
        parser.error("this profile combines a Linux and a Darwin observation")
    if args.peer_observations and not args.peer_observations.exists():
        parser.error("peer observations path does not exist")
    other = "darwin" if sys.platform == "linux" else "linux"
    spec = importlib.util.spec_from_file_location("code_reader_index", LAB / "code-readers-index.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    rows = json.loads((LAB / "starter/sample/tests.json").read_bytes())["tests"]
    candidates = builder.candidate_sites(rows)
    single_match = False
    for n in sorted(candidates):
        local = destination(n, sys.platform)
        local.parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local.parent) as temp:
            produced = Path(temp) / "report.json"
            subprocess.run([sys.executable, "-B", str(LAB / "code-readers-probe.py"), "--base", str(LAB / "source"),
                            "--out", str(produced), rows[n]["id"]], check=True)
            report = json.loads(produced.read_bytes())
            if report["node"] != rows[n]["id"] or report["platform"] != sys.platform:
                raise ValueError("local probe identity mismatch")
            produced.replace(local)
        if args.peer_observations is None:
            continue
        peer = peer_file(args.peer_observations, n, other)
        if peer is None:
            continue
        data = peer.read_bytes()
        report = json.loads(data)
        if report.get("platform") != other:
            parser.error(f"expected a genuine {other} observation at {peer}")
        if report.get("node") != rows[n]["id"]:
            if args.peer_observations.is_file():
                continue
            parser.error(f"wrong manifest node in {peer}")
        single_match = True
        target = destination(n, other)
        target.parent.mkdir(exist_ok=True)
        if peer.resolve() != target.resolve():
            with tempfile.TemporaryDirectory(dir=target.parent) as temp:
                copy = Path(temp) / "report.json"
                copy.write_bytes(data)
                copy.replace(target)
    if args.peer_observations and args.peer_observations.is_file() and not single_match:
        parser.error("the peer report does not correspond to a candidate manifest node")
    builder.pack()


if __name__ == "__main__":
    main()
