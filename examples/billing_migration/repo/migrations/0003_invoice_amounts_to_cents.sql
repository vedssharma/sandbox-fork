-- 0003: store invoice amounts as integer cents (the payments provider rejects fractional amounts).
ALTER TABLE invoices ADD COLUMN amount_cents bigint;

UPDATE invoices SET amount_cents = (amount::numeric(12, 2) * 100)::bigint;

ALTER TABLE invoices ALTER COLUMN amount_cents SET NOT NULL;
ALTER TABLE invoices ADD CONSTRAINT invoices_amount_cents_nonneg CHECK (amount_cents >= 0);
ALTER TABLE invoices DROP COLUMN amount;
