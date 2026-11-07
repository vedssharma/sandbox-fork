"""tinykv 1.x: a tiny file-per-key string store. Values are stored as latin-1."""

import os

__version__ = "1.4.2"
FORMAT = "1"


class Store:
    def __init__(self, path):
        self.path = path
        os.makedirs(path, exist_ok=True)
        fmt = os.path.join(path, "FORMAT")
        if not os.path.exists(fmt):
            with open(fmt, "w") as f:
                f.write(FORMAT)

    def _file(self, key):
        return os.path.join(self.path, key.encode("utf-8").hex() + ".v")

    def get(self, key):
        try:
            with open(self._file(key), "rb") as f:
                return f.read().decode("latin-1")
        except FileNotFoundError:
            return None

    def put(self, key, value):
        with open(self._file(key), "wb") as f:
            f.write(value.encode("latin-1"))

    def delete(self, key):
        try:
            os.remove(self._file(key))
        except FileNotFoundError:
            pass

    def keys(self):
        return sorted(
            bytes.fromhex(n[:-2]).decode("utf-8") for n in os.listdir(self.path) if n.endswith(".v")
        )

    def close(self):
        pass


def connect(path):
    return Store(path)
