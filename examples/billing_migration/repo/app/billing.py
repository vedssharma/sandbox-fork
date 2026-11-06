"""Billing views over the customers and invoices tables."""

from app.db import query


def invoice_header(invoice_id: int) -> str:
    rows = query(
        "SELECT i.id, c.name, i.amount FROM invoices i JOIN customers c ON c.id = i.customer_id"
        " WHERE i.id = :'p1'::int",
        invoice_id,
    )
    if not rows:
        raise KeyError(invoice_id)
    inv, name, amount = rows[0]
    return f"INV-{int(inv):05d} | {name} | ${float(amount):,.2f}"


def customer_balance_cents(customer_id: int) -> int:
    rows = query("SELECT coalesce(sum(amount), 0) FROM invoices WHERE customer_id = :'p1'::int", customer_id)
    return round(float(rows[0][0]) * 100)
