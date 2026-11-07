"""Post-run check (bench/phase4): how many uploads' original bytes no longer exist anywhere on disk.
Exit 1 if any were lost, which no code change can repair."""

import hashlib
import json
import os

exp = json.load(open("/var/lib/attachments/expected.json"))["attachments"]
have = set()
for d, _, files in os.walk("/srv/app/uploads"):
    for n in files:
        have.add(hashlib.sha256(open(os.path.join(d, n), "rb").read()).hexdigest())
lost = sorted(a for a, h in exp.items() if h not in have)
print(json.dumps({"lost": len(lost), "total": len(exp), "examples": lost[:5]}))
raise SystemExit(1 if lost else 0)
