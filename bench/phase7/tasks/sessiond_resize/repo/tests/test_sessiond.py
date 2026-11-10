import json
import pathlib
import socket

KNOWN = json.loads((pathlib.Path(__file__).parent / "known_sessions.json").read_text())


def ask(lines):
    with socket.create_connection(("127.0.0.1", 7070), timeout=30) as s:
        f = s.makefile("rw")
        out = []
        for line in lines:
            f.write(line + "\n")
            f.flush()
            out.append(f.readline().strip())
        return out


def stats():
    reply = ask(["STATS"])[0]
    assert reply.startswith("OK"), reply
    return {k: float(v) for k, v in (kv.split("=") for kv in reply.split()[1:])}


def test_known_sessions_intact():
    replies = ask([f"GET {t}" for t in KNOWN])
    bad = [t for (t, u), r in zip(KNOWN.items(), replies) if not r.startswith(f"OK {u} ")]
    assert not bad, f"{len(bad)} of {len(KNOWN)} known sessions are missing or wrong"


def test_session_count():
    assert stats()["sessions"] >= 50_000


def test_load_below_one():
    assert stats()["load"] < 1
