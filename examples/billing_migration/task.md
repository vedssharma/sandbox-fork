Rehearse the deploy of migration `0003_invoice_amounts_to_cents` on this machine, then make the deploy safe.

Our new payments provider only accepts integer amounts, so 0003 replaces the legacy `invoices.amount` column (dollars) with `amount_cents`. A teammate wrote and reviewed it. This machine is a disposable rehearsal environment holding a copy of production (Postgres, running locally; `psql` connects with no arguments), and the point of a rehearsal is to find out what the deploy really does to production data.

1. Rehearse first: run `./migrate.sh` with the migration exactly as committed, the same way the deploy pipeline will, before changing anything. Then run `python3 tests/test_billing.py`, which checks every invoice against values recorded from production before the migration.
2. If the rehearsal shows a problem, fix the migration so the real deploy is safe. When you finish, the database here must be in the state the fixed deploy produces on an untouched copy of production.
3. Update `app/billing.py` to the new column. `invoice_header` must keep returning exactly what it returns today, and `customer_balance_cents` must return the exact sum of the customer's invoices in cents.
4. `python3 tests/test_billing.py` must pass. Don't edit the tests.
