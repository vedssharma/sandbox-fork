"""SQLite connection for the attachments service."""

import sqlite3

DB_PATH = "/srv/app/app.db"


def connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)
