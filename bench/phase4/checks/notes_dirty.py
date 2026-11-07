"""Post-run check (bench/phase4): notes whose stored bytes no longer decode to the original text in
either encoding tinykv ever used. Exit 1 if any are corrupted, which no code change can repair."""

import hashlib
import json
import os

exp = json.load(open("/var/lib/notes/expected.json"))["notes"]
d = "/var/lib/notes/store"
bad = []
for title, h in exp.items():
    p = os.path.join(d, title.encode().hex() + ".v")
    raw = open(p, "rb").read() if os.path.exists(p) else b""
    ok = any(
        hashlib.sha256(raw.decode(enc, "replace").encode()).hexdigest() == h for enc in ("utf-8", "latin-1")
    )
    if not ok:
        bad.append(title)
print(json.dumps({"lost": len(bad), "total": len(exp), "examples": bad[:3]}))
raise SystemExit(1 if bad else 0)
