"""Simulated users logging in and reading their sessions (runs next to sessiond)."""

import random
import socket
import time

rng = random.Random()
while True:
    try:
        with socket.create_connection(("127.0.0.1", 7070), timeout=5) as s:
            f = s.makefile("rw")
            f.write(f"LOGIN u{rng.randint(1, 20000)}\n")
            f.flush()
            f.readline()
    except OSError:
        pass
    time.sleep(1)
