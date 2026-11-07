"""0003: move uploads from one flat folder into a folder per owner.

    uploads/<id>-<filename>  ->  uploads/<owner>/<filename>

The new CDN treats object keys case-insensitively, so file names are normalised to lower case.
Reviewed-by: dana
"""

import os
import shutil
import sqlite3

DB = "/srv/app/app.db"
UPLOADS = "/srv/app/uploads"

db = sqlite3.connect(DB)
rows = db.execute("SELECT id, owner, filename, path FROM attachments ORDER BY id").fetchall()
for attachment_id, owner, filename, path in rows:
    folder = os.path.join(UPLOADS, owner)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, filename.lower())
    shutil.move(path, dest)
    db.execute("UPDATE attachments SET path = ? WHERE id = ?", (dest, attachment_id))
db.commit()
print(f"moved {len(rows)} uploads")
