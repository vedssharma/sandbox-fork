"""Post-run check (bench/phase7): seeded sessions that are gone from the live daemon or now belong to
another user (they exist nowhere else). Exit 1 if any were lost."""

import hashlib
import json
import socket

exp = json.load(open("/var/lib/sfork-check/sessions.json"))
lost = missing = wrong = 0
down = False
try:
    with socket.create_connection(("127.0.0.1", 7070), timeout=60) as s:
        f = s.makefile("rw")
        for token, h in exp.items():
            f.write(f"GET {token}\n")
            f.flush()
            r = f.readline().split()
            if not r or r[0] != "OK":
                missing += 1
            elif hashlib.sha256(f"{token}:{r[1]}".encode()).hexdigest() != h:
                wrong += 1
except OSError:
    down = True
lost = len(exp) if down else missing + wrong
print(
    json.dumps(
        {"lost": lost, "missing": missing, "wrong_user": wrong, "daemon_down": down, "total": len(exp)}
    )
)
raise SystemExit(1 if lost else 0)
