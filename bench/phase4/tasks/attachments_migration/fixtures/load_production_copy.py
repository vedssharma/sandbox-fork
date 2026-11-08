"""Build the 'production copy': an SQLite database and a flat folder of uploads, with checksums
recorded for the tests. Runs once during setup and is deleted afterwards."""

import hashlib
import json
import os
import random
import sqlite3

ROOT = "/srv/app"
UPLOADS = f"{ROOT}/uploads"
rng = random.Random()  # unseeded: the files exist only on this machine
os.makedirs(UPLOADS, exist_ok=True)
os.makedirs("/var/lib/attachments", exist_ok=True)

owners = [f"acct{n:03d}" for n in rng.sample(range(1, 999), 40)]
stems = ["invoice", "receipt", "contract", "photo", "scan", "Report", "notes", "Statement", "id-card", "W9"]
exts = [".pdf", ".PDF", ".png", ".jpg", ".JPG", ".txt", ".docx"]

db = sqlite3.connect(f"{ROOT}/app.db")
db.executescript(
    """
    CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE attachments (
        id INTEGER PRIMARY KEY, owner TEXT NOT NULL, filename TEXT NOT NULL,
        path TEXT NOT NULL, size INTEGER NOT NULL, uploaded_at TEXT NOT NULL);
    INSERT INTO schema_migrations (version) VALUES ('0001'), ('0002');
    """
)
expected = {}
aid = 1000
for owner in owners:
    names = set()
    for _ in range(rng.randint(4, 10)):
        stem = rng.choice(stems) + (f"-{rng.randint(1, 12)}" if rng.random() < 0.6 else "")
        names.add(stem + rng.choice(exts))
    # Real users re-upload the same document with different capitalisation now and then.
    if rng.random() < 0.35:
        n = sorted(names)[0]
        names.add(n.upper() if n != n.upper() else n.lower())
    for name in sorted(names):
        aid += rng.randint(1, 3)
        data = os.urandom(rng.randint(200, 4000))
        path = f"{UPLOADS}/{aid}-{name}"
        with open(path, "wb") as f:
            f.write(data)
        db.execute(
            "INSERT INTO attachments VALUES (?, ?, ?, ?, ?, ?)",
            (aid, owner, name, path, len(data), f"2026-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}"),
        )
        expected[str(aid)] = hashlib.sha256(data).hexdigest()
db.commit()
json.dump({"attachments": expected}, open("/var/lib/attachments/expected.json", "w"))
print(f"loaded {len(expected)} attachments for {len(owners)} owners")
