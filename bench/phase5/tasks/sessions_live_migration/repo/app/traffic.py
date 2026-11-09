"""Simulated users logging in (runs next to the service: `python3 -m app.traffic`)."""

import random
import time
import urllib.request

rng = random.Random()
while True:
    prefix = "L" if rng.random() < 0.2 else "U"
    user = f"{prefix}-{rng.randint(1, 3000):06d}"
    try:
        req = urllib.request.Request(f"http://127.0.0.1:8080/login?user={user}", method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass
    time.sleep(2)
