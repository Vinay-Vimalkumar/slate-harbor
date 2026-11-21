"""Select baseline readers, directory enumerators and failed lookups for added files."""
import gzip
import json
from pathlib import PurePosixPath


class PathObservers:
    def __init__(self, data):
        self.dirs = data["dirs"]
        self.paths = data["paths"]
        self.groups = data["sets"]
        self.unknown = set(data["unknown"])
        self.files = set(data["files"])
        self.metadata_sites = data.get("metadata_sites")
        self.metadata_users = None
        self.execution_sites = data.get("execution_sites")

    @classmethod
    def load(cls, path, rows):
        if not path.is_file():
            return None
        with gzip.open(path, "rt") as src:
            data = json.load(src)
        if data["nodes"] == [r["id"] for r in rows] and "files" in data:
            return cls(data)
        return None

    def consumers(self, path):
        location = PurePosixPath(path)
        parent = location.parent.as_posix()
        module = (path.startswith("sqlglot/") and location.suffix == ".py"
                  and location.stem.isidentifier() and not location.stem.startswith("__")
                  and f"{parent}/__init__.py" in self.files
                  and location.with_suffix("").as_posix() not in self.files)
        fixture = path.startswith("tests/fixtures/") and location.suffix in {".sql", ".json", ".txt"}
        if not (module or fixture) or parent not in self.files:
            return None
        included = set(self.unknown)
        for key, mapping in ((path, self.paths), (parent, self.dirs)):
            if key in mapping:
                included.update(self.groups[mapping[key]])
        return included

    def content_observers(self, path, isolated, *, removed=False):
        location = PurePosixPath(path)
        fixture = path.startswith("tests/fixtures/") and location.suffix in {".sql", ".txt", ".json"}
        document = (location.suffix in {".md", ".rst"} or location.name in {"LICENSE", "Makefile", "py.typed"}
                    or path.startswith(".github/workflows/") and location.suffix in {".yaml", ".yml"})
        if not (fixture or document) or not isolated or self.metadata_sites is None:
            return None
        if self.metadata_users is None:
            self.metadata_users = set()
            for source, lines in self.metadata_sites.items():
                for line in lines:
                    self.metadata_users.update(isolated.at(source, line))
        known = self.groups[self.paths[path]] if path in self.paths else ()
        users = self.unknown | (self.metadata_users & set(known))
        parent = location.parent.as_posix()
        if removed and parent in self.dirs:
            users.update(self.groups[self.dirs[parent]])
        return users

    def mode_observers(self, path, isolated):
        if not isolated or self.execution_sites is None or self.metadata_sites is None:
            return None
        included = set(self.unknown)
        # A mode change can affect metadata queries or launching a program.
        # Opening a regular file's unchanged Python source does not consume +x.
        for sites in (self.metadata_sites, self.execution_sites):
            for source, lines in sites.items():
                for line in lines:
                    included.update(isolated.at(source, line))
        return included
