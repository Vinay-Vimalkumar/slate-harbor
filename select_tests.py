import argparse
import gzip
import hashlib
import heapq
import json
from pathlib import Path
import time

from fixture_verify import classify
from impact import Catalog
from isolated import Isolated
from patches import parse_patch, UnsupportedPatch
from relations import Relations
from verify import verify


def pinned_catalog(rows):
    if any(row["number"] != n or row["id"].split("::", 1)[0] != row["file"]
           for n, row in enumerate(rows)):
        return False
    names = "\n".join(row["id"] for row in rows).encode()
    return hashlib.sha256(names).hexdigest() == "97a4a74dd286c8864c0093b8b09b312879a64fe9fa85e783a233206caa88aa13"


def schedule(tests):
    ordered = sorted(tests, key=lambda test: (-test[1], test[0]))
    best_cost = None
    best_groups = []
    for count in range(1, min(8, len(ordered)) + 1):
        loads = [(0, worker) for worker in range(count)]
        groups = [[] for _ in range(count)]
        for number, millis in ordered:
            elapsed, worker = loads[0]
            groups[worker].append(number)
            heapq.heapreplace(loads, (elapsed + millis, worker))
        used = [group for group in groups if group]
        cost = max(elapsed for elapsed, _ in loads) + 2000 * len(used)
        if best_cost is None or cost < best_cost:
            best_cost, best_groups = cost, used
    return best_groups


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--base", type=Path, default=Path("/opt/base"))
    parser.add_argument("--verification-budget", type=float, default=0)
    args = parser.parse_args()
    deadline = time.monotonic() + min(args.verification_budget, 125)
    records = json.loads((args.data / "tests.json").read_bytes())["tests"]
    weights = {row["number"]: int(row["seconds"] * 1000 + 0.5) for row in records}
    patches = sorted((args.data / "commits").glob("*.patch"))
    if not pinned_catalog(records):
        groups = schedule(weights.items())
        for patch in patches:
            print(json.dumps({"commit": patch.stem, "workers": groups}, separators=(",", ":")))
        return
    with gzip.open(Path(__file__).with_name("coverage-index.json.gz"), "rt") as src:
        index = json.load(src)
    isolated = Isolated.load(Path(__file__).with_name("isolated-index.gz"), records)
    catalog = Catalog(records, index, isolated)
    relations = Relations.load(Path(__file__).with_name("relation-index.json.gz"), records)
    for number, patch in enumerate(patches):
        picks = catalog.all
        try:
            changes = parse_patch(patch.read_bytes(), args.base)
            picks = catalog.pick(changes)
            revised = {c.path: c.after for c in changes}
            if len(changes) == 1 and changes[0].path.startswith("tests/fixtures/optimizer/"):
                assessment = classify(revised, (catalog.rows[n] for n in sorted(picks)), base=args.base)
                picks = (picks - assessment.passed) | assessment.failed
            if relations:
                readers = set(catalog.fallback)
                for change in changes:
                    readers.update(index["reads"].get(change.path, ()))
                    if isolated:
                        readers.update(isolated.reads(change.path))
                picks = picks - relations.proven(changes, picks, readers=readers)
            if args.verification_budget > 0 and 0 < len(picks) <= 100:
                fraction = (deadline - time.monotonic()) / (len(patches) - number)
                if fraction >= 1.0:
                    passes = verify(revised,
                                    [catalog.rows[n] for n in sorted(picks)],
                                    deadline=time.monotonic() + fraction, base=args.base)
                    picks = picks - passes
        except (UnsupportedPatch, OSError, UnicodeError, RecursionError):
            picks = catalog.all
        groups = schedule([(n, weights[n]) for n in picks])
        print(json.dumps({"commit": patch.stem, "workers": groups}, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
