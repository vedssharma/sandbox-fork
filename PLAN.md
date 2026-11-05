# sandbox-fork: Plan

**Fork-on-Action and Speculative Exploration for coding agents, built on Daytona VM sandboxes.**

Status: planning (no code yet). Last updated 2026-11-05.

---

## 1. The idea in one paragraph

Agents do better when they can try several approaches and keep the best one, but a normal container only lets them move forward. If an agent edits a schema migration, runs it, and breaks the database, `git reset` restores the source files but not the database, the installed packages, the build cache, or the background processes. sandbox-fork gives an agent an **execution tree**: at any decision point the running sandbox (disk, memory, and processes) is forked into N identical children, each child tries one approach, an evaluator runs the tests and benchmarks in every child, the winner becomes the new head, and the losers are deleted. The same machinery provides **checkpoints**: the agent (or the orchestrator, automatically) can save the full machine state before a risky step and rewind to it later without rebuilding the environment.

## 2. What Daytona gives us today (and what it doesn't)

Researched against Daytona's docs and changelog in November 2026. The short version: **the hardest part of this idea, a whole-machine fork with memory, is now a stable Daytona primitive**, so we do not need to build CRIU or copy-on-write filesystems ourselves for the core loop.

| Capability | Daytona support | Notes |
|---|---|---|
| Fork a running sandbox (disk + memory + processes) | Yes, **Linux VM class only** | `sandbox.fork(name=...)` (Python), `sandbox.fork({ name })` (TS). Promoted from experimental to stable in v0.202.0 (2026-07-29). Containers cannot fork. |
| Fork of a fork | Yes | Trees of arbitrary depth. API exposes `/forks` (children), `/parent`, `/ancestors`. |
| Hot snapshot (disk + memory) | Yes, Linux VM only, REST only | `POST /api/sandbox/{id}/snapshot` with `includeMemory: true`. SDKs currently only make cold snapshots. New sandboxes created from it start with processes already running. |
| Cold snapshot (disk only) | Yes, all classes | Sandbox must be stopped. `sandbox.create_snapshot("name")`. |
| Pause / resume with memory | Yes, VM only | Resume is advertised as under one second. Paused sandboxes release CPU/RAM quota. |
| Volumes | Yes | S3-backed shared mounts. **Not forked** with the sandbox; shared across branches, so treat them as read-only caches. |
| Exec, files, git, sessions | Yes | Toolbox API: `sandbox.process.exec`, `sandbox.fs.*`, `sandbox.git.*`, long-running sessions. |
| Per-sandbox size | Up to 4 vCPU / 8 GiB / 10 GiB disk by default | Default VM snapshots: `daytona-vm-small/medium/large`. Custom VM snapshots can be built from a base image with `SandboxClass.LINUX_VM`. |
| Org quota | Tier 1: 10 vCPU / ~10-20 GiB; Tier 2: 100 vCPU / 200 GiB | Branch fan-out is bounded by this. Tier 2 needs a card and a $25 top-up. |
| Rate limits | Creation 300-600/min, lifecycle 10k-50k/min | Fork/snapshot don't list a separate limit; expect 429s and back off. |

**Unknowns we must measure in Phase 0** (not documented anywhere I could find):

1. Fork latency as a function of RAM in use and disk dirtied. Is it copy-on-write (fast, constant) or a full copy (slow, grows with size)?
2. Hot-snapshot latency and restore latency. This decides whether "checkpoint before every risky command" is affordable.
3. Max practical tree depth and fan-out before latency or storage degrades.
4. What a fork does to networking: same IP? Are open TCP connections duplicated? Is the guest told it was forked?
5. Billing of paused parents and snapshots (storage cost of keeping a deep tree around).
6. Constraint already documented: **a parent cannot be deleted while it has active fork children.** This shapes garbage collection (section 4.4).

**What we still build ourselves**

- The orchestrator: the execution tree, scheduling, budgets, scoring, promotion, and garbage collection. This is the product.
- Agent integration: forking the agent's *conversation state* together with the sandbox, and giving the agent fork/checkpoint/rewind tools.
- A guest agent ("post-fork hook") inside the VM that fixes the things a memory fork makes identical: RNG state, `machine-id`, hostnames, stale connections, clocks.
- Optional fine-grained checkpoints *inside* one VM (section 4.6), only if Phase 0 shows Daytona hot snapshots are too slow for per-command checkpoints.

## 3. Architecture

```
                         ┌──────────────────────────────────────────────┐
  CLI / REST / MCP  ───▶ │                Orchestrator                  │
                         │  ┌────────────┐ ┌───────────┐ ┌───────────┐  │
                         │  │ Tree store │ │ Scheduler │ │ Evaluator │  │
                         │  │ (Postgres) │ │ + budgets │ │ + scoring │  │
                         │  └────────────┘ └───────────┘ └───────────┘  │
                         │  ┌───────────────────┐ ┌──────────────────┐  │
                         │  │ Agent runtime(s)  │ │ Daytona adapter  │  │
                         │  │ (one per branch)  │ │ (fork/snap/exec) │  │
                         │  └─────────┬─────────┘ └────────┬─────────┘  │
                         └────────────┼────────────────────┼────────────┘
                                      │ tool calls         │ SDK / REST
                                      ▼                    ▼
                         ┌───────────────────────────────────────────────┐
                         │ Daytona Linux VM sandboxes                    │
                         │   root ──fork──▶ A ──fork──▶ A1, A2           │
                         │        └─fork──▶ B (loser, deleted)           │
                         │   each VM: repo, Postgres, deps, guest agent  │
                         └───────────────────────────────────────────────┘
```

Key choice: **the agent loop runs outside the sandbox, in the orchestrator.** The sandbox is just a machine the agent drives through tools. That makes forking the agent trivial (copy its message history and pin it to the child sandbox), keeps API keys out of the VMs, and lets one process run many branches concurrently.

### 3.1 Core concepts

- **Run**: one task given to the system ("make the failing migration test pass").
- **Node**: one sandbox at one point in time. Fields: `id`, `run_id`, `parent_id`, `daytona_sandbox_id`, `kind` (`root | branch | checkpoint`), `status` (`running | paused | evaluating | won | lost | failed | gc`), `hypothesis` (the approach this branch is trying), `agent_state_ref`, `created_at`, cost counters.
- **Fork point**: a node with 2+ children, plus the reason it forked and the list of alternatives.
- **Checkpoint**: a node frozen as a restore point (paused fork or hot snapshot), not actively worked on.
- **Head**: the node currently considered the best state of the run. Promotion moves the head.
- **Evaluation**: a scored record of running the eval suite in a node.

## 4. Orchestrator design

### 4.1 The fork-evaluate-promote loop

```
on fork_request(node P, alternatives [a1..aN]):
    check budget (branches, depth, $ , wall clock)            # refuse or trim N
    pause agent on P; flush guest state (sync, checkpoint DB WAL)
    children = parallel( daytona.fork(P) for each ai )         # backoff on 429
    for each child: run guest post-fork hook; clone agent state; inject hypothesis ai
    P -> status=paused (it is now a checkpoint)
    run branch agents concurrently, each with a step/time/token budget
    as each branch reports done (or exhausts budget): evaluate(child)
    early-stop: kill branches that are strictly dominated once the leader is clear
    winner = select(scores)
    promote(winner); mark others lost; delete losers
```

### 4.2 Who decides to fork

Three sources, all ending in the same `fork_request`:

1. **Agent-initiated** (primary): the agent calls a `fork` tool with 2-4 named approaches when it sees a real design choice. Prompting matters here; we will tune the tool description so the agent forks on genuine alternatives, not trivia.
2. **Orchestrator-initiated sampling**: at the start of a run (or after a failed attempt) spawn N branches with the same task and different hypotheses or temperatures. This is "best-of-N with real environments" and is the easiest thing to benchmark.
3. **Policy-initiated checkpoints**: before commands that match risky patterns (`migrate`, `alembic upgrade`, `rm -rf`, `pip install`/`npm install` with version changes, `DROP`, `git push --force`) the orchestrator transparently checkpoints first. No branching, just a cheap undo point.

### 4.3 Budgets and scheduling

- Hard caps per run: max live branches (bounded by Daytona quota), max depth, max total sandbox-minutes, max LLM tokens/$, wall clock.
- Scheduler is a simple priority queue over runnable branches; priority = current score estimate + exploration bonus. Start with fixed fan-out and successive halving (run all N for k steps, evaluate, keep the top half, continue). Something MCTS-like can come later if data says it helps.
- Paused nodes cost storage only, so "park" branches that are behind instead of deleting them immediately; delete at end of run.

### 4.4 Promotion and garbage collection

Because a parent can't be deleted while it has live children, the tree can only be cleaned up from the leaves inward:

1. Delete losers immediately ("vaporize").
2. The winner becomes head; its parent stays paused as a checkpoint (useful for rewinds anyway).
3. When the chain of ancestors gets long (configurable, e.g. depth > 5) or at the end of a run: **compact** by taking a hot snapshot of the head, creating a fresh root from it, and then deleting the whole old chain from the leaves up.
4. At end of run, the **deliverable** is extracted from the head: a git branch/patch pushed to the target repo, plus the evaluation report. The sandbox itself is a means, not the output.

### 4.5 Rewind ("step backward in execution time")

- `checkpoint(label)` → pause-fork or hot snapshot of the current node; returns a checkpoint id.
- `rewind(checkpoint_id)` → fork a new live node from that checkpoint (never mutate the checkpoint itself, so you can rewind to the same point many times), discard the current node, and rewind the agent's conversation to the matching message index, with a short note about what was tried and why it failed so the agent doesn't repeat it.
- Rewinding is just "fork from an older node", so it reuses all of the same machinery.

### 4.6 Fine-grained checkpoints inside one VM (only if needed)

If Phase 0 shows Daytona hot snapshots take seconds-to-minutes, checkpointing before every risky command becomes too slow. Fallback layers, all inside the VM:

- **Filesystem**: put the workspace and database data directories on btrfs (or ZFS) inside the guest and take subvolume snapshots, which are near-instant copy-on-write. Rollback = swap subvolume and restart the affected service. Cheap and robust; covers the schema-migration case well.
- **Processes**: CRIU for specific long-running processes (dev server, REPL) if they need memory-exact restore. CRIU is fragile with sockets, GPUs, and some runtimes, so treat it as optional and per-process, not whole-machine.
- **Database**: for Postgres specifically, `CREATE DATABASE ... TEMPLATE` or a pg_basebackup/WAL position is another cheap checkpoint.

The design keeps one `Checkpoint` interface with two backends (`daytona_hot` and `in_vm_cow`) so the orchestrator doesn't care which is used.

### 4.7 Fork hygiene (guest agent)

A memory fork produces machines that are identical in ways that cause subtle bugs. A small guest daemon (or a script the orchestrator runs right after `fork`) handles:

- Reseeding the kernel RNG and any long-lived user-space PRNGs that matter (warn: we can't reach inside every process).
- Regenerating `/etc/machine-id` and hostname where tools care.
- Resetting outbound connections (e.g. restarting a connection pool) if forks share an IP or duplicate TCP state.
- Clock sync after resume.
- Reporting "ready" so the orchestrator knows the child is usable.

### 4.8 Side effects outside the sandbox

Forking only isolates what's inside the VM. Rule: **everything stateful the agent touches must live in the sandbox** (Postgres, Redis, queues as local services in the VM image). Egress is restricted to package registries, the LLM API (called from the orchestrator anyway), and an allowlist. Daytona volumes are shared across branches, so mount them read-only (caches, datasets). Any unavoidable external call goes through an orchestrator tool that records it in a side-effect ledger and is disabled in non-head branches.

## 5. Agent integration

### 5.1 Agent runtime

- **Model**: Claude via the Anthropic API with tool use. Use the strongest model for the planner/head (Opus 5.5, `claude-opus-5-5`) and a cheaper one for parallel branch workers (Sonnet 5.5, `claude-sonnet-5-5`) once we have data on whether it matters. Haiku 5.5 for cheap judging/summarizing.
- **Loop**: our own thin tool-use loop (or the Claude Agent SDK) running in the orchestrator, one instance per branch. Each branch's agent state = message history + scratch notes + pointer to its node.
- **Forking the agent**: copy the message history at the fork point, append a message like "You are branch B. Pursue approach: *Pattern B: ...*. Other branches are trying the alternatives; don't hedge." Prompt caching makes the shared prefix cheap across branches.

### 5.2 Tools exposed to the agent

| Tool | Effect |
|---|---|
| `bash(cmd, timeout)` | `sandbox.process.exec` in the branch's sandbox (policy hook may auto-checkpoint first) |
| `read_file`, `write_file`, `edit_file`, `list_dir` | Toolbox fs API |
| `run_tests(selector?)` | Runs the configured eval suite, returns parsed results |
| `fork(alternatives: [{name, plan}])` | Requests a fork; the calling branch becomes the parent checkpoint |
| `checkpoint(label)` | Saves a restore point |
| `rewind(checkpoint_id, lesson)` | Returns to a checkpoint, carrying a short lesson forward |
| `done(summary)` | Signals the branch is ready to be evaluated |

### 5.3 MCP server

Expose the same tools (plus `run`, `status`, `tree`) as an MCP server, so any MCP-capable agent, including Claude Code, can use sandbox-fork as its execution backend without us owning the agent loop. This is likely the best adoption path and costs little extra once the orchestrator API exists.

## 6. Evaluating and scoring branches

Evaluation runs inside each branch's sandbox (so it sees that branch's DB and deps) via a per-project config file, e.g. `sandbox-fork.yaml`:

```yaml
setup: ["make deps", "make db"]
gates:                          # must pass or the branch is disqualified
  - name: build
    cmd: make build
  - name: migrations_apply_cleanly
    cmd: alembic upgrade head && alembic downgrade -1 && alembic upgrade head
tests:
  cmd: pytest -q --junitxml=/tmp/junit.xml
  report: junit:/tmp/junit.xml
benchmarks:
  - name: p95_latency_ms
    cmd: ./bench.sh
    parse: json:.p95
    direction: lower
weights: { tests: 0.6, perf: 0.2, diff_size: 0.1, lint: 0.1 }
judge: { enabled: true, on: tie }   # LLM judge only breaks near-ties
```

Scoring:

1. **Gates**: build, typecheck, migrations reversible, no new failing tests that passed at the fork point. Fail any gate → score 0.
2. **Tests**: pass rate on the target tests, plus a regression penalty on previously passing tests. Run flaky-prone suites 2-3 times and use the median.
3. **Performance**: benchmark metrics normalized against the fork-point baseline (so "latency" means relative change, not raw numbers across different machines). Run on identically sized sandboxes.
4. **Change quality**: diff size, lint/static-analysis findings, files touched outside the expected area.
5. **LLM judge** (optional): only when the top two are within a margin, a judge model compares the two diffs against the task and returns a preference with reasons. Keep it out of the main score so results stay reproducible.

Every evaluation stores raw outputs (junit, bench JSON, diff) in object storage so a human can audit why a branch won.

## 7. Stack recommendation

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.12, asyncio** | Daytona's Python SDK is first-class and has an async client; best ecosystem for agent loops, eval parsing, and the Anthropic SDK. (TypeScript is a fine alternative if you'd rather share code with the UI.) |
| Sandboxes | **Daytona Linux VM class** | Only class with fork + memory snapshots. Build a custom VM snapshot per target project with deps and services preinstalled. |
| Orchestrator API | FastAPI + Pydantic | Typed REST + SSE/WebSocket event stream for the UI. |
| State | **Postgres** (SQLite for local dev) via SQLAlchemy/SQLModel | Tree nodes, runs, evaluations, ledger. Recursive CTEs make tree queries easy. |
| Artifacts | S3-compatible object storage (or local disk in dev) | Logs, junit, diffs, transcripts. |
| Workflow durability | In-process asyncio for MVP; **Temporal** later if runs need to survive orchestrator restarts | Don't pay Temporal's complexity until runs are long and valuable. |
| LLM | Anthropic API (Opus 5.5 head, Sonnet 5.5 workers, Haiku 5.5 utility), prompt caching on | |
| Agent interface | Internal tool loop + **MCP server** | MCP lets Claude Code and other agents use it directly. |
| CLI | Typer | `sfork run`, `sfork tree`, `sfork rewind`, `sfork gc`. |
| UI (Phase 5) | Next.js + React Flow | Live execution-tree view: nodes colored by status/score, click for diff, logs, transcript. |
| Observability | OpenTelemetry traces (one span per node/tool call), structured logs, cost counters | Fork/eval timing and $ per run are the core product metrics. |
| Testing | pytest + a fake Daytona adapter for unit tests; a small nightly suite against real Daytona | |

## 8. Proposed repo layout

```
sandbox-fork/
  PLAN.md
  pyproject.toml
  sfork/
    core/          # Run, Node, Checkpoint models; tree store; budgets
    daytona/       # adapter: fork, hot/cold snapshot, pause, exec, delete (+ fake)
    orchestrator/  # scheduler, fork-evaluate-promote loop, gc/compaction
    agent/         # tool-use loop, tool definitions, agent-state forking
    eval/          # config loader, gate/test/bench runners, scoring, judge
    guest/         # post-fork hook script + guest daemon shipped in the VM image
    api/           # FastAPI app, event stream
    mcp/           # MCP server
    cli.py
  images/          # VM snapshot definitions for target projects
  examples/        # demo repos: schema-migration task, refactor A vs B task
  bench/           # harness comparing linear vs branching agents
  tests/
```

## 9. Phased roadmap

Durations assume one person working steadily; adjust freely.

**Phase 0: Spikes and measurements (≈1 week).** Goal: replace every "unknown" in section 2 with a number.
- Script that creates a Linux VM sandbox, loads it (Postgres + repo + 2 GiB RAM in use), and measures fork, hot snapshot, restore, pause/resume latency across sizes; fan-out of 2/4/8; depth of 1/5/10.
- Check fork networking, RNG/`machine-id` duplication, and parent-deletion constraints.
- Check cost of keeping paused nodes/snapshots.
- Decision out of this phase: is Daytona hot snapshot fast enough for automatic checkpoints, or do we need the in-VM btrfs layer (4.6)?

**Phase 1: Tree engine + CLI, no agent (≈2 weeks).**
- Data model, Daytona adapter (+ fake), fork/checkpoint/rewind/promote/gc, guest post-fork hook.
- `sfork` CLI to drive it by hand: `sfork fork <node> --n 2`, `sfork exec <node> "cmd"`, `sfork rewind`, `sfork tree`.
- Done when: you can break a database migration in one branch, keep the other, and rewind with no leftover state.

**Phase 2: Evaluator (≈1-2 weeks).**
- `sandbox-fork.yaml`, gate/test/bench runners, junit parsing, baseline-relative scoring, artifact storage.
- Done when: given N branches, it picks the right winner on the demo repos and explains why.

**Phase 3: Agent integration (≈2 weeks).**
- Tool-use loop with the tools in 5.2, agent-state forking, policy auto-checkpoints, budgets, successive halving.
- Done when: the agent autonomously forks on "Pattern A vs Pattern B" in the refactor demo and the evaluator promotes the better branch.

**Phase 4: Benchmark the claim (≈1-2 weeks).**
- Compare linear agent vs best-of-N vs agent-initiated forking at **equal $ budget** on a SWE-bench-style subset plus our own stateful tasks (migrations, dependency upgrades). Track solve rate, wall clock, $ per solve, and "dirty-state failures" avoided.
- This tells us whether the idea pays for itself and where.

**Phase 5: Interfaces (≈2 weeks).**
- REST API + event stream, MCP server, React Flow tree UI.

**Phase 6: Hardening.**
- Temporal (if needed), compaction tuning, quota-aware scheduling across concurrent runs, security review of egress/side-effect policy, docs.

## 10. Risks and how we handle them

| Risk | Mitigation |
|---|---|
| Fork or hot snapshot is too slow for interactive use | Phase 0 measures it first; in-VM btrfs/CRIU fallback; prefer pause-forks for checkpoints. |
| Quota caps fan-out (Tier 1 = 10 vCPU) | Use small VMs for branches; park paused branches; plan for Tier 2 early. |
| Forked VMs share hidden state (RNG, IDs, connections) | Guest post-fork hook; test for it explicitly in Phase 0. |
| External side effects escape the tree | Everything stateful in-VM; egress allowlist; side-effect ledger. |
| Agents fork too much or too little | Budgets; tool-description tuning; measure in Phase 4. |
| Flaky tests pick the wrong winner | Repeat runs, median, gates on regressions, judge only for ties. |
| Daytona API changes / VM class limits (no GPU fork, no declarative builder for VMs) | Thin adapter layer; keep a fake for tests; build VM images from base images. |
| Cost of N× LLM calls | Prompt caching on the shared prefix; cheaper worker model; successive halving. |

## 11. Open questions for you

1. **Target workloads**: is the first target your own repos, SWE-bench-style benchmarks, or a product for other people's agents? This decides whether the MCP server or the internal agent comes first.
2. **Python or TypeScript** for the orchestrator? I recommend Python.
3. **Daytona tier**: are you on Tier 1? Real fan-out testing really wants Tier 2.
4. **Agent**: build our own loop on the Anthropic API, or drive Claude Code through the MCP server from day one?

## Sources

- Daytona docs, Sandboxes (lifecycle states, feature matrix by class): https://www.daytona.io/docs/en/sandboxes/
- Daytona docs, Linux sandboxes (fork, hot/cold snapshots): https://www.daytona.io/docs/en/linux-sandboxes/
- Daytona docs, Persistence (volumes, snapshots, fork parent rules): https://www.daytona.io/docs/en/persistence/
- Daytona docs, Limits (tiers, quotas, rate limits): https://www.daytona.io/docs/en/limits/
- Daytona changelog, "Stable sandbox fork and snapshot creation" (v0.202.0): https://www.daytona.io/changelog/stable-sandbox-fork-and-snapshot-creation
- Daytona dotfiles, "VMs (Pause & Fork)": https://www.daytona.io/dotfiles/vms-pause-and-fork
