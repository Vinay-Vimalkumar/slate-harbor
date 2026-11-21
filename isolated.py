"""Read compact dependency bitsets measured in separate test processes."""
from functools import lru_cache
import gzip
import marshal
from pathlib import Path


class Isolated:
    def __init__(self, index):
        self.lines = index["lines"]
        self.masks = index["masks"]
        self.read_masks = index["reads"]
        self.module_masks = index["modules"]
        self.unknown = self.numbers(index["missing"] | index["uncertain"])

    @staticmethod
    @lru_cache(maxsize=4096)
    def numbers(mask):
        out = set()
        while mask:
            last = mask & -mask
            out.add(last.bit_length() - 1)
            mask ^= last
        return frozenset(out)

    def reads(self, path):
        return self.numbers(self.masks[self.read_masks.get(path, 0)])

    def imports(self, path):
        return self.numbers(self.masks[self.module_masks.get(path, 0)])

    def at(self, path, line):
        return self.numbers(self.masks[self.lines.get(path, {}).get(line, 0)])

    def span(self, path, first, last):
        result = 0
        for n, slot in self.lines.get(path, {}).items():
            if first <= n <= last:
                result |= self.masks[slot]
        return self.numbers(result)

    @classmethod
    def load(cls, path, rows):
        if not path.is_file():
            return None
        with gzip.open(path, "rb") as f:
            index = marshal.loads(f.read())
        if index["version"] == 1 and index["nodes"] == [r["id"] for r in rows]:
            return cls(index)
        return None
