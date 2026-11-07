# Same fix as round-cast, but the migration runs in one transaction and refuses to drop the old
# column unless every converted amount matches it to the cent.
set -euo pipefail
cat > migrations/0003_invoice_amounts_to_cents.sql <<'SQL'
-- 0003: store invoice amounts as integer cents (the payments provider rejects fractional amounts).
-- One transaction: if the check below fails, nothing changes and the legacy column survives.
BEGIN;
ALTER TABLE invoices ADD COLUMN amount_cents bigint;

-- Cast through float8: real -> numeric keeps only 6 significant digits and corrupts amounts >= $10k.
UPDATE invoices SET amount_cents = round(amount::float8 * 100)::bigint;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM invoices
        WHERE amount_cents IS NULL OR abs(amount_cents - amount::float8 * 100) >= 0.5
    ) THEN
        RAISE EXCEPTION '0003: converted amounts do not match the legacy column; aborting';
    END IF;
END
$$;

ALTER TABLE invoices ALTER COLUMN amount_cents SET NOT NULL;
ALTER TABLE invoices ADD CONSTRAINT invoices_amount_cents_nonneg CHECK (amount_cents >= 0);
ALTER TABLE invoices DROP COLUMN amount;
COMMIT;
SQL
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
