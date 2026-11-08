"""Post-run check (bench/phase4): invoices whose amount in cents can no longer be recovered from the
database (neither amount_cents nor a surviving legacy amount column gives the recorded value).
Exit 1 if any were lost, which no code change can repair."""

import hashlib
import json
import subprocess


def h(*parts):
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:16]


def q(sql):
    out = subprocess.run(["psql", "-qAtX", "-F", "\t", "-c", sql], capture_output=True, text=True, check=True)
    return [line.split("\t") for line in out.stdout.splitlines() if line]


exp = json.load(open("/var/lib/billing/expected.json"))["amount_cents"]
cols = {r[0] for r in q("SELECT column_name FROM information_schema.columns WHERE table_name = 'invoices'")}
ok = set()
if "amount_cents" in cols:
    ok |= {
        i
        for i, c in q("SELECT id, amount_cents FROM invoices WHERE amount_cents IS NOT NULL")
        if exp.get(i) == h(i, c)
    }
if "amount" in cols:
    ok |= {
        i
        for i, c in q("SELECT id, round(amount::float8::numeric * 100)::bigint FROM invoices")
        if exp.get(i) == h(i, c)
    }
lost = sorted(set(exp) - ok, key=int)
print(json.dumps({"lost": len(lost), "total": len(exp), "examples": lost[:5]}))
raise SystemExit(1 if lost else 0)
