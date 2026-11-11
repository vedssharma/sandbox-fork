# sessiond runbook

sessiond (vendor binary, `/usr/local/bin/sessiond`) keeps every login session in memory, in one hash
table. Restarting it logs everyone out, so it is never restarted in production; all maintenance is
done online through its admin protocol (`bin/sessctl`).

## RB-14: resize the session table

When `sessctl stats` shows `load` above 4, lookups get slow. Resize the table online:

1. `bin/sessctl stats`
2. `bin/sessctl resize N`, with N the next power of two that is at least 16 x the session count
   (for production's ~50k sessions that's 1048576), so we don't have to do this again soon.
3. `bin/sessctl stats` should show the same session count and a load below 1.
