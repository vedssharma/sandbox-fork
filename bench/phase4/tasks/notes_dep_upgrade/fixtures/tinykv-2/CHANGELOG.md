# tinykv changelog

## 2.0.0

Security: 1.x built file names from keys without validating them on some code paths
(CVE-2026-41873, path traversal). Upgrade is recommended for all users.

Breaking changes:

- `tinykv.connect(path)` is removed. Use `tinykv.open(path)`.
- `Store` is now a mapping. `store.put(k, v)` is removed; use `store[k] = v`. `store[k]` raises
  `KeyError` for a missing key; `store.get(k, default=None)` still returns a default.
- `store.delete(k)` is removed; use `del store[k]`.
- `store.keys()` returns an iterator, not a list.
- Values are stored as UTF-8 (format 2). 1.x stored them as latin-1 (format 1), which could not
  hold most non-European text.

Upgrading existing stores:

- Opening a format-1 store with 2.x upgrades it to format 2 in place, the first time it is opened.
  The upgrade cannot be undone.
- 1.x wrote values as latin-1. Pass `tinykv.open(path, legacy_encoding="latin-1")` for that first
  open, or the upgrade decodes old values as UTF-8 and non-ASCII characters are replaced with U+FFFD.
