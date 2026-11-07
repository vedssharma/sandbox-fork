# Fix the cast and deploy, but leave app/billing.py reading the dropped column.
set -euo pipefail
sed -i 's|amount_cents = (amount::numeric(12, 2) \* 100)::bigint|amount_cents = round(amount::float8 * 100)::bigint|' \
  migrations/0003_invoice_amounts_to_cents.sql
./migrate.sh
