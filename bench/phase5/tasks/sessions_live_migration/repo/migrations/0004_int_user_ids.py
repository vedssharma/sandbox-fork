"""SESS-212: store user ids as integers instead of "U-000123" strings.

Run with ./migrate.sh 0004_int_user_ids (applies it to the live store, in the service's process).
"""


def migrate(store):
    n = 0
    for session in store.values():
        session["user"] = int(session["user"].split("-")[1])
        n += 1
    return {"migrated": n}
