"""The notes service's storage layer, on top of tinykv."""

import tinykv

STORE_PATH = "/var/lib/notes/store"


def _store():
    return tinykv.connect(STORE_PATH)


def get_note(title):
    """The note's body, or None if there is no such note."""
    return _store().get(title)


def save_note(title, body):
    _store().put(title, body)


def delete_note(title):
    _store().delete(title)


def titles(prefix=""):
    return [t for t in _store().keys() if t.startswith(prefix)]


def word_count(title):
    body = get_note(title)
    return 0 if body is None else len(body.split())
