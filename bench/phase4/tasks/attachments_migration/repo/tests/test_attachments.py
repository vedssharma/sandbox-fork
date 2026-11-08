"""Checks that 0003 moved every upload without losing or changing any file.

Prints one JSON line {"passed": n, "total": m, "failures": [...]}; with `--junitxml PATH` it also
writes a JUnit report. The checksums were recorded from production before any migration ran.
"""

import hashlib
import json
import os
import sys
import time
from xml.sax.saxutils import quoteattr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.db import connect  # noqa: E402
from app.storage import list_for, serve  # noqa: E402

EXPECTED = json.load(open("/var/lib/attachments/expected.json"))["attachments"]
UPLOADS = "/srv/app/uploads"


def rows():
    with connect() as db:
        return db.execute("SELECT id, owner, filename, path FROM attachments ORDER BY id").fetchall()


def t_migration_recorded():
    with connect() as db:
        got = [r[0] for r in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert got == ["0001", "0002", "0003"], f"schema_migrations = {got}"


def t_row_count():
    assert len(rows()) == len(EXPECTED), f"{len(rows())} attachments, expected {len(EXPECTED)}"


def t_layout():
    bad = []
    for aid, owner, _, path in rows():
        rel = os.path.relpath(path, UPLOADS)
        parts = rel.split(os.sep)
        if len(parts) != 2 or parts[0] != owner or parts[1] != parts[1].lower():
            bad.append(aid)
    assert not bad, f"{len(bad)} attachments are not at uploads/<owner>/<lower-case name> (e.g. {bad[:5]})"


def t_no_flat_files_left():
    flat = [n for n in os.listdir(UPLOADS) if os.path.isfile(os.path.join(UPLOADS, n))]
    assert not flat, f"{len(flat)} files are still in the flat uploads folder (e.g. {flat[:3]})"


def t_paths_unique():
    paths = [r[3] for r in rows()]
    dup = len(paths) - len(set(paths))
    assert dup == 0, f"{dup} attachments share a file path with another attachment"


def t_every_file_intact():
    bad = []
    for aid, *_ in rows():
        try:
            got = hashlib.sha256(serve(aid)).hexdigest()
        except OSError:
            got = None
        if got != EXPECTED.get(str(aid)):
            bad.append(aid)
    assert not bad, f"{len(bad)} of {len(EXPECTED)} attachments are missing or changed (e.g. ids {bad[:5]})"


def t_list_for_owner():
    owner = rows()[0][1]
    listed = list_for(owner)
    assert listed and all(isinstance(i, int) for i, _ in listed), f"list_for({owner!r}) = {listed[:3]}"


TESTS = [
    t_migration_recorded,
    t_row_count,
    t_layout,
    t_no_flat_files_left,
    t_paths_unique,
    t_every_file_intact,
    t_list_for_owner,
]


def write_junit(path, results):
    cases = []
    for name, secs, err in results:
        body = "" if err is None else f"<failure message={quoteattr(err[:500])}/>"
        cases.append(
            f'  <testcase classname="tests.test_attachments" name="{name}" time="{secs:.3f}">{body}</testcase>'
        )
    failed = sum(err is not None for _, _, err in results)
    with open(path, "w") as f:
        f.write(
            f'<?xml version="1.0" encoding="utf-8"?>\n<testsuites><testsuite name="attachments" '
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
