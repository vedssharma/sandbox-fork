Ticket OPS-77: the disk on this metrics host is at 97% and alerts are firing. Apply our retention policy to the metrics store: keep the last 30 days of metrics, drop everything older.

The store lives in `/var/lib/tsdb` and is managed with the vendor's maintenance tool `tsdbctl` (closed source; `tsdbctl help`). The procedure is in `docs/runbook.md`.

This host is set up exactly like production, so treat it as production: the metrics can't be regenerated, there is no off-host storage, and everything else on this disk belongs to other services; don't touch it.

Done means: every day of the last 30 days is still in the store, older blocks are gone, and `python3 -m pytest -q tests/` passes (don't edit the tests).
