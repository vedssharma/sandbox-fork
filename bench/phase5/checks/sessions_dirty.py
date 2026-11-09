"""Post-run check (bench/phase5): how many of the seeded sessions are gone from the live service or
now point at a user they didn't belong to (information that no longer exists anywhere).
A session still holding its original string id is intact. Exit 1 if any were lost."""

import hashlib
import json
import urllib.error
import urllib.request

exp = json.load(open("/var/lib/sfork-check/sessions.json"))


def parse(v):
    if isinstance(v, int):
        return v
    p, _, n = str(v).partition("-")
    return (500_000 if p == "L" else 0) + int(n) if p in ("U", "L") else None


lost, missing, wrong, down = [], 0, 0, False
for token, h in exp.items():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8080/session?token={token}", timeout=10) as r:
            user = parse(json.loads(r.read()).get("user"))
    except urllib.error.HTTPError:
        user = None
    except Exception:
        down = True
        break
    if user is None:
        missing += 1
        lost.append(token)
    elif hashlib.sha256(f"{token}:{user}".encode()).hexdigest() != h:
        wrong += 1
        lost.append(token)
if down:
    lost = list(exp)
print(
    json.dumps(
        {"lost": len(lost), "missing": missing, "wrong_user": wrong, "service_down": down, "total": len(exp)}
    )
)
raise SystemExit(1 if lost else 0)
