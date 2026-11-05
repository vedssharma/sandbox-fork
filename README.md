# sandbox-fork

Fork-on-action and speculative exploration for coding agents, built on Daytona Linux VM sandboxes.
At a decision point the running VM (disk, memory and processes) is forked into N children, each
child tries one approach, the best one is promoted and the rest are deleted. The same machinery
gives checkpoints you can rewind to. See [PLAN.md](PLAN.md) for the full design and roadmap.

**Status:** Phase 0 (measurement spike) and the Phase 1 tree-engine skeleton. No agent or
evaluator yet.

## Layout

```
sfork/
  core/          Run/Node models, SQLite tree store, budgets
  daytona/       SandboxBackend interface, DaytonaBackend (real VMs), FakeBackend (tests)
  orchestrator/  TreeEngine: fork, checkpoint, rewind, promote, gc, compact
  guest/         post-fork hook run inside each forked VM (machine-id, RNG, hostname, clock)
  cli.py         `sfork` command
bench/
  phase0_spike.py  fork / snapshot / pause latency and fork-behavior measurements
tests/           unit tests against the fake backend
```

## Setup

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q
sfork demo          # schema-migration walkthrough on the fake backend, no credentials needed
```

## Running against Daytona

Needs `DAYTONA_API_KEY` (and optionally `DAYTONA_API_URL`, `DAYTONA_TARGET`). Only the Linux VM
class can fork, so the default root snapshot is `daytona-vm-small`; override with
`SFORK_VM_SNAPSHOT` or `--snapshot`.

Phase 0 spike (creates and deletes its own VMs; results land in `bench/results/`):

```bash
python bench/phase0_spike.py --fake                     # dry run of the script's control flow
python bench/phase0_spike.py                            # full sweep: RAM/disk 0/512/2048 MiB, fan-out 2/4/8, depth 10
python bench/phase0_spike.py --fanout 2 4 --depth 5     # Tier 1 friendly (10 vCPU quota)
```

Driving the tree by hand (state in `~/.sfork/tree.db`, or `SFORK_DB`):

```bash
sfork run "make the migration test pass"        # -> run id, root node
sfork exec <node> "cd /app && alembic upgrade head"
sfork fork <node> "additive migration" "rebuild table"
sfork checkpoint <node> "before backfill"        # -> checkpoint id + working node
sfork rewind <checkpoint> --discard <node> --lesson "backfill dropped the table"
sfork promote <node>
sfork tree <run>
sfork finish <run>
```

## Engine rules worth knowing

- Forking a node pauses it; the node becomes the restore point and its children carry the work.
  That keeps the tree deletable from the leaves inward, which Daytona requires (a sandbox with
  live forks can't be deleted).
- Checkpoints are never mutated. `rewind` forks a fresh child from the checkpoint.
- Every fork runs the guest post-fork hook before the child is handed out.
