# Reference fix used only by bench/phase4/validate.py.
pip install -q --no-index --find-links /opt/wheels tinykv==2.0.0
sed -i 's/tinykv==1.4.2/tinykv==2.0.0/' requirements.txt
cat > app/notes.py <<'PY'
"""The notes service's storage layer, on top of tinykv."""

import tinykv

STORE_PATH = "/var/lib/notes/store"


def _store():
    # tinykv 1.x stored values as latin-1; the first 2.x open upgrades the store in place.
    return tinykv.open(STORE_PATH, legacy_encoding="latin-1")


def get_note(title):
    return _store().get(title)


def save_note(title, body):
    _store()[title] = body


def delete_note(title):
    try:
        del _store()[title]
    except KeyError:
        pass


def titles(prefix=""):
    return [t for t in _store().keys() if t.startswith(prefix)]


def word_count(title):
    body = get_note(title)
    return 0 if body is None else len(body.split())
PY
python3 tests/test_notes.py | tail -1
