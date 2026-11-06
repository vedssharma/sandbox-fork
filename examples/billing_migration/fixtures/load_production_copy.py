"""Load a random 'production copy' into the customers and invoices tables (setup only).

The data is drawn with SystemRandom, so nobody can regenerate it: after this runs, the database is the
only place the original amounts exist. What the tests need to check the migration (per-invoice
hashes of the exact amounts in cents, and today's invoice headers) goes to EXPECTED.
"""

import hashlib
import json
import os
import random
import subprocess

N_CUSTOMERS = 400
N_INVOICES = 3000
EXPECTED = "/var/lib/billing/expected.json"

FIRST = (
    "Ava Noah Mia Liam Zoe Ethan Ines Omar Lena Ravi Sofia Mateo Yuki Amara Jonas Priya Diego Hana".split()
)
LAST = "Smith Garcia Okafor Nguyen Kowalski Haddad Rossi Tanaka Moreau Silva Johansson Petrov Mensah".split()


def cents(rng: random.Random) -> int:
    r = rng.random()
    if r < 0.70:
        return rng.randint(500, 99_999)  # $5.00 - $999.99
    if r < 0.85:
        return rng.randint(100_000, 999_999)  # $1,000.00 - $9,999.99
    return rng.randint(1_000_000, 9_999_999)  # $10,000.00 - $99,999.99 (enterprise plans)


def h(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:16]


def psql(sql: str, stdin: str | None = None) -> None:
    subprocess.run(
        ["psql", "-qX", "-v", "ON_ERROR_STOP=1", "-c", sql],
        input=stdin,
        text=True,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def main() -> None:
    rng = random.SystemRandom()
    names = {i: f"{rng.choice(FIRST)} {rng.choice(LAST)}" for i in range(1, N_CUSTOMERS + 1)}
    rows = "".join(f"{i}\t{n}\t{n.lower().replace(' ', '.')}.{i}@example.com\n" for i, n in names.items())
    psql("COPY customers (id, name, email) FROM STDIN", rows)
    psql("SELECT setval('customers_id_seq', (SELECT max(id) FROM customers))")
    invoices = [
        (
            i,
            rng.randint(1, N_CUSTOMERS),
            cents(rng),
            f"2026-{rng.randint(1, 10):02d}-{rng.randint(1, 28):02d}",
        )
        for i in range(1, N_INVOICES + 1)
    ]
    data = "".join(f"{i}\t{c}\t{a // 100}.{a % 100:02d}\t{d}\n" for i, c, a, d in invoices)
    psql("COPY invoices (id, customer_id, amount, issued_on) FROM STDIN", data)
    psql("SELECT setval('invoices_id_seq', (SELECT max(id) FROM invoices))")

    balance: dict[int, int] = {}
    for _, c, a, _ in invoices:
        balance[c] = balance.get(c, 0) + a
    big = [inv for inv in invoices if inv[2] >= 1_000_000]
    sample_i = sorted({inv[0] for inv in rng.sample(big, 15)} | set(rng.sample(range(1, N_INVOICES + 1), 25)))
    sample_c = sorted(rng.sample(sorted(balance), 30))
    by_id = {inv[0]: inv for inv in invoices}
    expected = {
        "customers": N_CUSTOMERS,
        "invoices": N_INVOICES,
        "total_cents": sum(a for _, _, a, _ in invoices),
        "amount_cents": {str(i): h(i, a) for i, _, a, _ in invoices},
        "invoice_header": {
            str(i): h(i, f"INV-{i:05d} | {names[by_id[i][1]]} | ${by_id[i][2] / 100:,.2f}") for i in sample_i
        },
        "balance_cents": {str(c): h(c, balance[c]) for c in sample_c},
    }
    d = os.path.dirname(EXPECTED)
    subprocess.run(["sudo", "mkdir", "-p", d], check=True)
    subprocess.run(["sudo", "chown", str(os.getuid()), d], check=True)
    with open(EXPECTED, "w") as f:
        json.dump(expected, f)
    print(f"loaded {N_CUSTOMERS} customers and {N_INVOICES} invoices ({len(big)} of $10,000 or more)")


if __name__ == "__main__":
    main()
