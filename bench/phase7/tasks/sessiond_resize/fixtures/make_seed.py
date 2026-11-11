"""Setup only (deleted afterwards): the session export sessiond loads once at startup (it deletes
the file), and the post-run check's expectations. Deterministic, so tests/known_sessions.json
matches it."""

import hashlib
import json
import os
import random

rng = random.Random(301)
seed = {}
for i in range(50_000):
    token = f"{rng.getrandbits(128):032x}"
    seed[token] = (f"u{rng.randint(1, 20000)}", 1_790_000_000 + i * 11)
with open("/var/lib/sessiond/seed.txt", "w") as f:
    for t, (u, c) in seed.items():
        f.write(f"{t} {u} {c}\n")
with open("/tmp/sessions-check.json", "w") as f:
    json.dump({t: hashlib.sha256(f"{t}:{u}".encode()).hexdigest() for t, (u, _) in seed.items()}, f)
if os.environ.get("SFORK_EMIT_KNOWN"):
    pick = sorted(seed)[:200]
    print(json.dumps({t: seed[t][0] for t in pick}, indent=0))
