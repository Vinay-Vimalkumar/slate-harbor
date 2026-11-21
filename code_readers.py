"""Code-object and source-file observations for fully audited pristine reader tests."""
import gzip
import hashlib
import json


class CodeReaders:
    def __init__(self, index):
        self.reviewed = frozenset(index["reviewed"])
        self.code = {p: frozenset(ns) for p, ns in index["code"].items()}
        self.raw = {p: frozenset(ns) for p, ns in index["raw"].items()}

    @classmethod
    def load(cls, path, rows):
        if not path.is_file():
            return None
        with gzip.open(path) as source:
            index = json.load(source)
        identity = hashlib.sha256("\n".join(r["id"] for r in rows).encode()).hexdigest()
        if (index.get("version") == 1 and index.get("catalog") == identity
                and index.get("revision") == "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"):
            return cls(index)
        return None

    def positions(self, path, blanket, imported=None):
        fallback = set(blanket) - self.reviewed
        if imported is not None:
            fallback.intersection_update(imported)
        return fallback | self.code.get(path, frozenset()) | self.raw.get(path, frozenset())

    def semantic(self, path):
        return self.code.get(path, frozenset()) | self.raw.get(path, frozenset())

    def source(self, path):
        return self.raw.get(path, frozenset())
