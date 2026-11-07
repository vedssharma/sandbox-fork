"""Checks the notes service after the tinykv 2.0 upgrade.

Prints one JSON line {"passed": n, "total": m, "failures": [...]}; with `--junitxml PATH` it also
writes a JUnit report. Checksums were recorded from the production store before the upgrade.
"""

import hashlib
import json
import os
import re
import sys
import time
from xml.sax.saxutils import quoteattr

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

EXPECTED = json.load(open("/var/lib/notes/expected.json"))["notes"]


def t_tinykv_2():
    import tinykv

    assert tinykv.__version__ == "2.0.0", f"tinykv {tinykv.__version__} is installed, expected 2.0.0"


def t_requirements_pinned():
    reqs = open(os.path.join(ROOT, "requirements.txt")).read()
    assert re.search(r"^tinykv==2\.0\.0\s*$", reqs, re.M), "requirements.txt should pin tinykv==2.0.0"


def t_every_note_intact():
    from app.notes import get_note

    bad = [t for t, h in EXPECTED.items() if hashlib.sha256((get_note(t) or "").encode()).hexdigest() != h]
    assert not bad, f"{len(bad)} of {len(EXPECTED)} notes have changed or are missing (e.g. {bad[:3]})"


def t_titles():
    from app.notes import titles

    assert sorted(titles("note-")) == sorted(EXPECTED), "titles('note-') doesn't list every note"


def t_missing_note_is_none():
    from app.notes import get_note, word_count

    assert get_note("no-such-note") is None
    assert word_count("no-such-note") == 0


def t_unicode_roundtrip():
    from app.notes import delete_note, get_note, save_note

    body = "東京 meeting — résumé ✓"
    save_note("zz-test-unicode", body)
    try:
        assert get_note("zz-test-unicode") == body, "a note with non-latin text didn't round-trip"
    finally:
        delete_note("zz-test-unicode")
    assert get_note("zz-test-unicode") is None, "delete_note didn't delete"


TESTS = [
    t_tinykv_2,
    t_requirements_pinned,
    t_every_note_intact,
    t_titles,
    t_missing_note_is_none,
    t_unicode_roundtrip,
]


def write_junit(path, results):
    cases = []
    for name, secs, err in results:
        body = "" if err is None else f"<failure message={quoteattr(err[:500])}/>"
        cases.append(
            f'  <testcase classname="tests.test_notes" name="{name}" time="{secs:.3f}">{body}</testcase>'
        )
    failed = sum(err is not None for _, _, err in results)
    with open(path, "w") as f:
        f.write(
            f'<?xml version="1.0" encoding="utf-8"?>\n<testsuites><testsuite name="notes" '
            f'tests="{len(results)}" failures="{failed}">\n'
            + "\n".join(cases)
            + "\n</testsuite></testsuites>\n"
        )


if __name__ == "__main__":
    failures, results = [], []
    for t in TESTS:
        t0 = time.monotonic()
        try:
            t()
            print(f"ok   {t.__name__}")
            results.append((t.__name__, time.monotonic() - t0, None))
        except Exception as e:
            failures.append(f"{t.__name__}: {e}")
            results.append((t.__name__, time.monotonic() - t0, f"{type(e).__name__}: {e}"))
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    if "--junitxml" in sys.argv:
        write_junit(sys.argv[sys.argv.index("--junitxml") + 1], results)
    print(json.dumps({"passed": len(TESTS) - len(failures), "total": len(TESTS), "failures": failures}))
