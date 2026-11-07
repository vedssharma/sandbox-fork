# Fix the cast (go through float8, round to whole cents), deploy, update the app.
set -euo pipefail
sed -i 's|amount_cents = (amount::numeric(12, 2) \* 100)::bigint|amount_cents = round(amount::float8 * 100)::bigint|' \
  migrations/0003_invoice_amounts_to_cents.sql
grep -q 'round(amount::float8 \* 100)' migrations/0003_invoice_amounts_to_cents.sql
cat > app/billing.py <<'EOF'
"""Billing views over the customers and invoices tables."""

from app.db import query


def invoice_header(invoice_id: int) -> str:
    rows = query(
        "SELECT i.id, c.name, i.amount_cents FROM invoices i JOIN customers c ON c.id = i.customer_id"
        " WHERE i.id = :'p1'::int",
        invoice_id,
    )
    if not rows:
        raise KeyError(invoice_id)
    inv, name, cents = rows[0]
    cents = int(cents)
    return f"INV-{int(inv):05d} | {name} | ${cents // 100:,}.{cents % 100:02d}"


def customer_balance_cents(customer_id: int) -> int:
    rows = query(
        "SELECT coalesce(sum(amount_cents), 0) FROM invoices WHERE customer_id = :'p1'::int", customer_id
    )
    return int(rows[0][0])
EOF
./migrate.sh
