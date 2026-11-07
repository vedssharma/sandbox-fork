"""Serving attachments."""

from .db import connect


def attachment_path(attachment_id: int) -> str:
    with connect() as db:
        row = db.execute("SELECT path FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    if row is None:
        raise KeyError(attachment_id)
    return row[0]


def serve(attachment_id: int) -> bytes:
    with open(attachment_path(attachment_id), "rb") as f:
        return f.read()


def list_for(owner: str) -> list[tuple[int, str]]:
    with connect() as db:
        return db.execute(
            "SELECT id, filename FROM attachments WHERE owner = ? ORDER BY id", (owner,)
        ).fetchall()
