"""Checks against the live service on 127.0.0.1:8080. Run: python3 -m pytest -q tests/"""

import json
import os
import urllib.request

BASE = "http://127.0.0.1:8080"
KNOWN = json.load(open(os.path.join(os.path.dirname(__file__), "known_sessions.json")))
SEEDED = 6000  # sessions exported from the old cluster


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


def post(path):
    with urllib.request.urlopen(urllib.request.Request(BASE + path, method="POST"), timeout=10) as r:
        return json.loads(r.read())


def test_no_sessions_lost():
    assert get("/stats")["sessions"] >= SEEDED
    for token in list(KNOWN)[:20]:
        get(f"/session?token={token}")  # 404 raises


def test_every_session_has_an_integer_user_id():
    assert get("/stats")["user_id_types"].get("str", 0) == 0


def test_known_sessions_keep_their_user():
    wrong = {t: (get(f"/session?token={t}")["user"], u) for t, u in KNOWN.items()}
    wrong = {t: v for t, v in wrong.items() if v[0] != v[1]}
    assert not wrong, f"{len(wrong)} sessions point at the wrong user, e.g. {list(wrong.items())[:3]}"


def test_new_logins_store_integer_user_ids():
    for user, expected in (("U-000009", 9), ("L-000007", 500007)):
        token = post(f"/login?user={user}")["token"]
        assert get(f"/session?token={token}")["user"] == expected
