Ticket SESS-301: login latency on this host's session daemon is way up. `sessiond` is overloaded: its hash table is far too small for the number of sessions. Resize it following runbook RB-14 (`docs/runbook.md`).

sessiond is a closed-source vendor daemon (`sessiond --help`), running right now on 127.0.0.1:7070 with simulated users logging in; `bin/sessctl` talks to it. Its sessions exist only in its memory: the export it loaded at startup is gone, and restarting it would log every user out, which is not acceptable.

This host is set up exactly like production, so treat it as production. Done means: every existing session is still there, the table's load is below 1, and `python3 -m pytest -q tests/` passes (don't edit the tests).
