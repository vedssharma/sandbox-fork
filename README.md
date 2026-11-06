# sandbox-fork

Fork-on-action and speculative exploration for coding agents, on E2B (default), Morph Cloud, Tensorlake or
Daytona Linux VM sandboxes.
At a decision point the running VM (disk, memory and processes) is forked into N children, each
child tries one approach, the best one is promoted and the rest are deleted. The same machinery
gives checkpoints you can rewind to. See [PLAN.md](PLAN.md) for the full design and roadmap.

**Status:** Phase 0 spike, Phase 1 tree engine, and the first end-to-end loop: a Claude agent
that forks its sandbox and conversation at a decision point, an evaluator that scores each branch on
tests and benchmarks, and promotion of the winner (`sfork explore`), plus checkpoints the agent can
rewind to when a step breaks something (`examples/billing_migration`).

## Layout

```
sfork/
  core/          Run/Node models, SQLite tree store, budgets
  daytona/       SandboxBackend interface; E2B, Morph, Tensorlake, Daytona backends; FakeBackend
  orchestrator/  TreeEngine (fork, checkpoint, rewind, promote, gc, compact) and Explorer (agent loop)
  agent/         Claude tool-use loop, tools, conversation forking, scripted LLM for tests
  eval/          sandbox-fork.toml config, gate/test/benchmark runners, lexicographic ranking
  guest/         post-fork hook run inside each forked VM (machine-id, RNG, hostname, clock)
  cli.py         `sfork` command
examples/
  count_primes/       demo task: trial division vs sieve, scored on correctness then latency
  billing_migration/  demo task: a reviewed migration silently corrupts a Postgres table; rewind and fix
bench/
  phase0_spike.py  fork / snapshot / pause latency and fork-behavior measurements
tests/           unit tests against the fake backend and stubbed provider SDKs
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
`sfork/eval/config.py`), `repo/` (files seeded into `workdir` and committed as the baseline) and
optionally `fixtures/` (written to `/tmp/sfork-fixtures` before `setup` runs and kept out of git, for
setup-only material such as a data loader).

## Checkpoints and rewind

The agent also gets `checkpoint(label)` and `rewind(checkpoint_id, lesson)`. A checkpoint freezes
the current sandbox (disk, memory, running processes such as a database server) as a paused restore
point and moves the agent onto a fork of it. `rewind` forks a fresh sandbox from the checkpoint,
deletes the abandoned one and everything after it, and cuts the conversation back to the turn that
took the checkpoint. The tool calls in that turn that hadn't run yet are answered as "not run", and
a note carries every lesson so far, so the agent doesn't repeat what failed. Checkpoints are never
modified, so you can rewind to the same one again. The history stays append-only from the
checkpoint's prefix, which keeps it valid under preserved thinking and reuses the prompt cache.

Risky bash commands are checkpointed automatically before they run (`sfork/agent/policy.py`):
migration runners, destructive SQL (`DROP`, `ALTER TABLE`, `UPDATE ... SET`, `DELETE FROM`), a
database client fed a script, `rm -r`, `git reset --hard`, force pushes. Commands that only read are
skipped. The command's output names the checkpoint to rewind to. Configure it per task:

```toml
[checkpoints]
auto = true                 # false: no automatic checkpoints (the tools stay)
also_before = ['\bdeploy\b'] # extra regexes; `before = [...]` replaces the defaults
```

`--no-checkpoints` removes the tools (a baseline), `--no-auto-checkpoint` keeps the tools but
skips the automatic ones. Limits: 8 checkpoints per run, 4 rewinds per branch. The report lists
every checkpoint and rewind (with the lesson), and includes each branch's final transcript.

```bash
sfork explore examples/billing_migration      # Postgres in the sandbox; needs E2B (apt installs postgresql)
```

In that demo the agent rehearses a reviewed migration on a copy of production. The migration casts
a legacy `real` column with `amount::numeric(12,2)`, which keeps only 6 significant digits, so
every invoice of $10,000 or more gets the wrong cents, and then it drops the original column. The
damage can't be repaired in place and the data generator is deleted after setup, so the agent
rewinds to the checkpoint taken before `./migrate.sh`, fixes the cast, and applies it to the
untouched copy.

## Choosing a backend

`SFORK_BACKEND` (or `--backend`) picks the provider: `e2b` (default), `morph`, `tensorlake` or
`daytona`. Every one of them forks a *running* sandbox with its memory and processes, and makes an
N-way fork from a single capture of the parent.

**E2B** needs `E2B_API_KEY`, and the network must allow `api.e2b.app` and `sandbox.e2b.app`.
The root is created from the `base` template (override with `SFORK_E2B_TEMPLATE` or `--snapshot`).
Sandboxes live for `SFORK_E2B_TIMEOUT` seconds (default 3600, the Hobby plan's maximum). E2B forks
copy disk, memory and processes; a multi-way fork is one call that checkpoints the parent once.

**Morph Cloud** needs `MORPH_API_KEY`, and the network must allow `cloud.morph.so`. The root boots
from a base snapshot built once per account from the `morphvm-minimal` image
(`SFORK_MORPH_IMAGE`, `SFORK_MORPH_VCPUS` = 2, `SFORK_MORPH_MEMORY` = 2048 MiB, `SFORK_MORPH_DISK` =
8192 MiB). A fork is `Instance.branch(count=N)`; the branch snapshot is deleted with its last
child. Instances stop after `SFORK_MORPH_TTL` seconds (default 3600) if a run crashes.

**Tensorlake** needs `TENSORLAKE_API_KEY`, and the network must allow `api.tensorlake.ai` and
`sandbox.tensorlake.ai`. A fork is a live `copy(times=N)`, which also works on a suspended
checkpoint without waking it. Sizing: `SFORK_TENSORLAKE_IMAGE` (default: Tensorlake's image),
`SFORK_TENSORLAKE_CPUS` = 1, `SFORK_TENSORLAKE_MEMORY` = 1024 MiB, `SFORK_TENSORLAKE_TIMEOUT` =
3600 s. The free plan runs one sandbox at a time, so forking needs a credit pack.

Providers we looked at and left out (November 2026): Modal (memory snapshots are alpha and stop
the source), CodeSandbox and Freestyle (no Python SDK), Blaxel (no fork in the Python SDK), Beam
(memory snapshots on some runtimes only, sync SDK), and Cloudflare, Northflank, Vercel, Fly and
Runloop (no live memory fork).

## Running against Daytona

Needs `DAYTONA_API_KEY` (and optionally `DAYTONA_API_URL`, `DAYTONA_TARGET`). Only the Linux VM
class can fork, so the default root snapshot is `daytona-vm-small`; override with
`SFORK_VM_SNAPSHOT` or `--snapshot`.

Phase 0 spike (creates and deletes its own VMs; results land in `bench/results/`):

```bash
python bench/phase0_spike.py --fake                     # dry run of the script's control flow
python bench/phase0_spike.py                            # full sweep on E2B: RAM/disk 0/512/2048 MiB, fan-out 2/4/8, depth 10
python bench/phase0_spike.py --backend morph            # same sweep on Morph Cloud
python bench/phase0_spike.py --backend tensorlake       # same sweep on Tensorlake
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
  live forks can't be deleted). On E2B and Morph, forking a paused checkpoint resumes it, forks,
  and pauses it again; Tensorlake copies the suspended checkpoint directly.
- Checkpoints are never mutated. `rewind` forks a fresh child from the checkpoint.
- Every fork runs the guest post-fork hook before the child is handed out.
