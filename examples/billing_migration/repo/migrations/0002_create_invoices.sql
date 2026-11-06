CREATE TABLE invoices (
    id          serial PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customers(id),
    amount      real NOT NULL CHECK (amount >= 0),  -- dollars
    issued_on   date NOT NULL
);
CREATE INDEX invoices_customer_id ON invoices (customer_id);
