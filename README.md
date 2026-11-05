# sandbox-fork

Fork-on-action and speculative exploration for coding agents, on E2B (default) or Daytona Linux VM sandboxes.
At a decision point the running VM (disk, memory and processes) is forked into N children, each
child tries one approach, the best one is promoted and the rest are deleted. The same machinery
gives checkpoints you can rewind to. See [PLAN.md](PLAN.md) for the full design and roadmap.

**Status:** Phase 0 spike, Phase 1 tree engine, and the first end-to-end loop: a Claude agent
that forks its sandbox and conversation at a decision point, an evaluator that scores each branch on
tests and benchmarks, and promotion of the winner (`sfork explore`).

## Layout

```
sfork/
  core/          Run/Node models, SQLite tree store, budgets
  daytona/       SandboxBackend interface, E2BBackend, DaytonaBackend, FakeBackend (tests)
  orchestrator/  TreeEngine (fork, checkpoint, rewind, promote, gc, compact) and Explorer (agent loop)
  agent/         Claude tool-use loop, tools, conversation forking, scripted LLM for tests
  eval/          sandbox-fork.toml config, gate/test/benchmark runners, lexicographic ranking
  guest/         post-fork hook run inside each forked VM (machine-id, RNG, hostname, clock)
  cli.py         `sfork` command
examples/
  count_primes/  demo task: trial division vs sieve, scored on correctness then latency
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

## The agent loop: `sfork explore`

```bash
export ANTHROPIC_API_KEY=... E2B_API_KEY=...
sfork explore examples/count_primes                      # agent decides when to fork
sfork explore examples/count_primes --orchestrator-fork  # fork the root into the task's [[alternatives]] first
sfork explore examples/count_primes --backend local      # no E2B; runs commands on THIS machine
```

What happens: the root sandbox is seeded with `repo/` (and a baseline git commit), the agent works
through `bash`/`read_file`/`write_file`/`run_tests`, and when it calls `fork` with 2-4 alternatives
the engine forks the sandbox once per alternative (the parent pauses as a checkpoint) and copies the
conversation into each child with a note naming its approach. Branches run concurrently until they
call `done` or hit their step/time budget. The evaluator then runs the task's gates, tests and
benchmarks in every branch and ranks them: gates first, then test pass rate, then benchmarks
(normalized against the best branch, median of repeats), so a faster branch never beats a more
correct one. The winner becomes the head, losers are deleted right away, and at the end the head's
`git diff` and a JSON report (tree, per-branch evaluations, why the winner won, token usage per
model) are written to `bench/results/`. Every sandbox is deleted when the run ends, including on
errors (`--keep-head` keeps the winner).

Defaults: `claude-opus-5-5` for the root and branches (`--worker-model` to change branches),
effort `medium`, adaptive thinking, prompt caching (branches share the fork-point prefix) and
server-side refusal fallbacks. History is append-only, so every branch stays valid under preserved
thinking. Forks nest up to `--max-fork-depth` (default 1: branches can't fork again).

The Anthropic SDK honors `ANTHROPIC_BASE_URL`. Some hosted environments (including Claude Code
cloud sessions) set it for their own use; if requests fail with an auth error there, run with
`env -u ANTHROPIC_BASE_URL sfork explore ...`.

A task directory holds `task.md` (the prompt), `sandbox-fork.toml` (eval config, see
`sfork/eval/config.py`) and `repo/` (files seeded into `workdir`).

## Choosing a backend

`SFORK_BACKEND` (or the spike's `--backend`) picks the provider: `e2b` (default) or `daytona`.

**E2B** needs `E2B_API_KEY`, and the network must allow `api.e2b.app` and `sandbox.e2b.app`.
The root is created from the `base` template (override with `SFORK_E2B_TEMPLATE` or `--snapshot`).
Sandboxes live for `SFORK_E2B_TIMEOUT` seconds (default 3600, the Hobby plan's maximum). E2B forks
copy disk, memory and processes; a multi-way fork is one call that checkpoints the parent once.

## Running against Daytona

Needs `DAYTONA_API_KEY` (and optionally `DAYTONA_API_URL`, `DAYTONA_TARGET`). Only the Linux VM
class can fork, so the default root snapshot is `daytona-vm-small`; override with
`SFORK_VM_SNAPSHOT` or `--snapshot`.

Phase 0 spike (creates and deletes its own VMs; results land in `bench/results/`):

```bash
python bench/phase0_spike.py --fake                     # dry run of the script's control flow
python bench/phase0_spike.py                            # full sweep on E2B: RAM/disk 0/512/2048 MiB, fan-out 2/4/8, depth 10
python bench/phase0_spike.py --backend daytona          # same sweep on Daytona VMs
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
  live forks can't be deleted). On E2B, forking a paused checkpoint resumes it, forks, and pauses
  it again.
- Checkpoints are never mutated. `rewind` forks a fresh child from the checkpoint.
- Every fork runs the guest post-fork hook before the child is handed out.
