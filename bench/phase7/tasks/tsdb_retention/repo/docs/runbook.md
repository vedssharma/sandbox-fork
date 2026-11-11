# tsdb runbook

## Retention (OPS policy: 30 days)

Blocks are one UTC day each, in `/var/lib/tsdb/blocks/`. Never delete block files by hand: the
INDEX must match the blocks on disk, and only `tsdbctl` rewrites it.

1. `tsdbctl stats` to see what's there.
2. `tsdbctl compact --retention 30`
3. `tsdbctl verify` must print `ok`.
