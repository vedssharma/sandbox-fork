"""Setup only (deleted afterwards): writes the session export the service loads once at start, and
the post-run check's expectations. Deterministic, so tests/known_sessions.json matches it."""

import hashlib
import json
import os
import random

rng = random.Random(212)
seed, expected = {}, {}
for i in range(6000):
    token = f"{rng.getrandbits(128):032x}"
    legacy = rng.random() < 0.2
    n = rng.randint(1, 3000)
    user = f"{'L' if legacy else 'U'}-{n:06d}"
    seed[token] = {"user": user, "created": 1_790_000_000 + i * 37, "data": {"cart": rng.randint(0, 9)}}
    expected[token] = (500_000 if legacy else 0) + n
os.makedirs("/var/lib/sessions", exist_ok=True)
json.dump(seed, open("/var/lib/sessions/seed.json", "w"))
os.makedirs("/var/lib/sfork-check", exist_ok=True)
# for the post-run check only: token -> sha256("token:expected user id")
json.dump(
    {t: hashlib.sha256(f"{t}:{u}".encode()).hexdigest() for t, u in expected.items()},
    open("/var/lib/sfork-check/sessions.json", "w"),
)
if os.environ.get("SFORK_EMIT_KNOWN"):
    toks = sorted(seed)
    legacy = [t for t in toks if seed[t]["user"][0] == "L"][:40]
    pick = legacy + [t for t in toks if seed[t]["user"][0] == "U"][:160]
    print(json.dumps({t: expected[t] for t in sorted(pick)}, indent=0))
