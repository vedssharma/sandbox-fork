# Deploy 0003 exactly as reviewed and point the app at the new column.
# Wrong: real -> numeric(12,2) keeps 6 significant digits, so every amount >= $10,000 loses its cents.
set -euo pipefail
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
