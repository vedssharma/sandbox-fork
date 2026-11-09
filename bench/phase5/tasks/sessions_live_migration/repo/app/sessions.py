"""In-memory session store.

Sessions live only in this process (persistence is SESS-180, not done yet). Restarting the service
logs every user out.
"""

import secrets
import time

# token -> {"user": <user id>, "created": <epoch seconds>, "data": {...}}
STORE: dict = {}


def new_session(user: str) -> str:
    token = secrets.token_hex(16)
    STORE[token] = {"user": user, "created": int(time.time()), "data": {}}
    return token


def get(token: str) -> "dict | None":
    return STORE.get(token)
