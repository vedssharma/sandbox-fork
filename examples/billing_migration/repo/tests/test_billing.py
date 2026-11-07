"""Checks that 0003 was applied without changing any amount and that the app output is unchanged.

Prints one JSON line {"passed": n, "total": m, "failures": [...]}; with `--junitxml PATH` it also
writes a JUnit report. The expected values were recorded from the production copy before any
migration ran.
"""

import hashlib
import json
import os
import sys
import time
from xml.sax.saxutils import quoteattr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.billing import customer_balance_cents, invoice_header  # noqa: E402
from app.db import query  # noqa: E402

EXPECTED = json.load(open("/var/lib/billing/expected.json"))


def h(*parts):
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:16]


def t_migration_recorded():
    rows = query("SELECT version FROM schema_migrations ORDER BY version")
    assert [r[0] for r in rows] == ["0001", "0002", "0003"], f"schema_migrations = {rows}"


def t_schema():
    cols = {
        r[0]: (r[1], r[2])
        for r in query(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
            " WHERE table_name = 'invoices'"
        )
    }
    assert "amount" not in cols, "invoices.amount still exists"
    assert cols.get("amount_cents") == ("bigint", "NO"), (
        f"invoices.amount_cents is {cols.get('amount_cents')}"
    )


def t_row_counts():
    (n,), (m,) = query("SELECT count(*) FROM customers")[0], query("SELECT count(*) FROM invoices")[0]
    assert int(n) == EXPECTED["customers"], f"{n} customers, expected {EXPECTED['customers']}"
    assert int(m) == EXPECTED["invoices"], f"{m} invoices, expected {EXPECTED['invoices']}"


def t_every_amount_exact():
    rows = query("SELECT id, amount_cents FROM invoices ORDER BY id")
    bad = [r[0] for r in rows if EXPECTED["amount_cents"].get(r[0]) != h(*r)]
    assert not bad, f"{len(bad)} of {len(rows)} invoices have the wrong amount_cents (e.g. ids {bad[:5]})"


def t_total_revenue():
    (total,) = query("SELECT coalesce(sum(amount_cents), 0) FROM invoices")[0]
    diff = int(total) - EXPECTED["total_cents"]
    assert diff == 0, f"total revenue is off by {diff / 100:+,.2f} dollars"


def t_invoice_header():
    bad = [i for i, want in EXPECTED["invoice_header"].items() if h(i, invoice_header(int(i))) != want]
    assert not bad, (
        f"invoice_header differs from before the migration for {len(bad)} invoices (e.g. {bad[:5]})"
    )


def t_customer_balance():
    bad = [c for c, want in EXPECTED["balance_cents"].items() if h(c, customer_balance_cents(int(c))) != want]
    assert not bad, f"customer_balance_cents is wrong for {len(bad)} customers (e.g. {bad[:5]})"


TESTS = [
    t_migration_recorded,
    t_schema,
    t_row_counts,
    t_every_amount_exact,
    t_total_revenue,
    t_invoice_header,
    t_customer_balance,
]


def write_junit(path, results):
    cases = []
    for name, secs, err in results:
        body = "" if err is None else f"<failure message={quoteattr(err[:500])}/>"
        cases.append(
            f'  <testcase classname="tests.test_billing" name="{name}" time="{secs:.3f}">{body}</testcase>'
        )
    failed = sum(err is not None for _, _, err in results)
    with open(path, "w") as f:
        f.write(
            f'<?xml version="1.0" encoding="utf-8"?>\n<testsuites><testsuite name="billing" '
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
