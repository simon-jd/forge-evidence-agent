import hashlib
from pathlib import Path

class ParseCache:
    def __init__(self, parser):
        self.parser = parser
        self.entries = {}

    def get(self, path):
        path = Path(path)
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        previous = self.entries.get(str(path))
        if previous and previous[0] == key:
            return previous[1]
        value = self.parser(path.read_text())
        self.entries[str(path)] = (key, value)
        return value
