"""tinykv 2.x: a tiny file-per-key string store. Values are stored as UTF-8.

See CHANGELOG.md for the changes from 1.x.
"""

import builtins
import os
import warnings

__version__ = "2.0.0"
FORMAT = "2"
_MISSING = object()


class Store:
    """A mapping of str -> str backed by one file per key."""

    def __init__(self, path, *, legacy_encoding=None):
        self.path = path
        os.makedirs(path, exist_ok=True)
        fmt_file = os.path.join(path, "FORMAT")
        fmt = builtins.open(fmt_file).read().strip() if os.path.exists(fmt_file) else None
        if fmt == "1":
            _upgrade(path, legacy_encoding or "utf-8")
        elif fmt is None:
            with builtins.open(fmt_file, "w") as f:
                f.write(FORMAT)

    def _file(self, key):
        return os.path.join(self.path, key.encode("utf-8").hex() + ".v")

    def __getitem__(self, key):
        try:
            with builtins.open(self._file(key), "rb") as f:
                return f.read().decode("utf-8")
        except FileNotFoundError:
            raise KeyError(key) from None

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key, value):
        tmp = self._file(key) + ".tmp"
        with builtins.open(tmp, "wb") as f:
            f.write(value.encode("utf-8"))
        os.replace(tmp, self._file(key))

    def __delitem__(self, key):
        try:
            os.remove(self._file(key))
        except FileNotFoundError:
            raise KeyError(key) from None

    def __contains__(self, key):
        return os.path.exists(self._file(key))

    def __iter__(self):
        return iter(
            sorted(bytes.fromhex(n[:-2]).decode("utf-8") for n in os.listdir(self.path) if n.endswith(".v"))
        )

    def keys(self):
        return iter(self)

    def items(self):
        return ((k, self[k]) for k in self)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _upgrade(path, encoding):
    """Rewrite a format-1 store as format 2 (UTF-8 values), in place."""
    for n in os.listdir(path):
        if n.endswith(".v"):
            p = os.path.join(path, n)
            raw = builtins.open(p, "rb").read()
            with builtins.open(p, "wb") as f:
                f.write(raw.decode(encoding, errors="replace").encode("utf-8"))
    with builtins.open(os.path.join(path, "FORMAT"), "w") as f:
        f.write(FORMAT)
    warnings.warn(f"tinykv: upgraded store {path} from format 1 to format 2", stacklevel=3)


def open(path, *, legacy_encoding=None):  # noqa: A001
    """Open (or create) a store. A format-1 store (written by tinykv 1.x) is upgraded in place;
    pass the encoding 1.x wrote its values in as `legacy_encoding` (see CHANGELOG.md)."""
    return Store(path, legacy_encoding=legacy_encoding)
