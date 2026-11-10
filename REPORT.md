# sandbox-fork: final report

November 5 to 10, 2026. Status: concluded.

## Summary

sandbox-fork set out to test one idea: if a coding agent can fork its whole running machine (disk,
memory and processes) at a decision point, try several approaches in parallel and keep the best,
it should solve more tasks, and the same machinery should let it undo mistakes that `git reset`
can't.

We built it, ran it on three sandbox providers, and benchmarked it over seven phases (about 370
agent runs and roughly $66 of model spend in the benchmarks). The results split cleanly:

- **Forking as a search strategy is a negative result.** Agents never forked on their own: 0 times
  in 86 runs that had the tool. When we forced forking or ran best-of-N, the extra solves were
  within noise and cost 1.7x to 3.4x as much per solve. A better branch selector didn't help,
  because on most failed tasks no branch was correct in the first place.
- **Checkpoint/rewind is a narrow positive result.** It is a cheap, reliable safety net when an
  agent runs an opaque, irreversible operation on state it can't back up. It matters mostly for
  weaker models: with rewind available, Haiku 5.5 went from 0/10 to 6/10 clean solves and from
  10/10 to 0/10 runs ending with lost data. Opus 5.5 almost never needed it. The tasks that showed
  this were built to favour rewind, so how often real work looks like them is still open.

The infrastructure itself works well. On E2B a full-VM fork takes about 1 s, an 8-way fan-out
about 0.6 s, and a rewind 1 to 5 s with running processes intact.

## 1. What was built

All code is in this repository (`sfork/`). Phases 6 and 7 live in open PRs #12 and #13.

| Piece | What it does | Where |
|---|---|---|
| Sandbox backends | One `SandboxBackend` interface over E2B (default), Morph Cloud, Tensorlake and Daytona, plus fake and local backends for tests | `sfork/daytona/` |
| Tree engine | Execution tree of VM nodes: fork, fork_many, checkpoint, rewind, promote, garbage-collect | `sfork/orchestrator/engine.py` |
| Post-fork hook | Gives each forked VM its own machine-id, hostname, RNG seed and clock | `sfork/guest/post_fork.sh` |
| Agent loop | Claude tool-use loop with `bash`, file tools, `fork`, `checkpoint`, `rewind`; conversation is forked with the VM; auto-checkpoints before risky commands | `sfork/agent/`, `sfork/orchestrator/explore.py` |
| Evaluator | Scores branches relative to the fork point: gates, regression check, JUnit tests, benchmarks, diff size, protected-file check, LLM judge on near-ties (asked in both orders) | `sfork/eval/` |
| Cost meter | Per-run $ meter with a hard cap enforced before each model call | `sfork/core/cost.py` |
| Benchmark harness | Linear, best-of-N, agent fork, forced fork and prompt-only control conditions at equal $ caps; hidden-test oracles; dirty-state checks | `bench/phase4` to `bench/phase7` |
| Repro selector | A writer agent generates reproduction checks from the issue to rank branches (off by default) | `sfork/eval/repro.py` (PR #12) |

Not built from the original plan (PLAN.md): the REST API, MCP server, tree UI, successive halving
and the hardening phase. The benchmark results made them not worth building.

## 2. Providers

The plan targeted Daytona's Linux VM sandboxes, but the account could not create VM sandboxes on
any tier, so we surveyed alternatives and wrote adapters for three providers with live memory
forking. All numbers below are from our Phase 0 spike (`bench/phase0_spike.py`).

| | E2B | Tensorlake | Morph Cloud |
|---|---|---|---|
| Fork, idle | 0.6 s | 1.9 to 2.3 s | 3.1 s |
| Fork with 128 MiB RAM + 256 MiB disk dirtied | 1.15 s | 2.2 s | 3.4 s |
| Fan-out 2 / 4 / 8 | 0.65 / 0.67 / 0.62 s | 2.5 / 3.1 / 4.4 s | 4.4 / 6.1 / 10.6 s |
| Fork depth 1 to 5 | flat, 0.7 to 0.9 s | flat, about 2 s | flat, about 3 s |
| Pause / resume | 0.2 / 0.2 s | 0.5 / 0.7 s | 5 to 6.5 / 1 s |
| Hot snapshot / restore | 0.3 / 0.4 to 0.7 s | 1.2 / 0.85 s | 2 / 1 to 1.5 s |
| Fork a paused sandbox | refused (adapter resumes, forks, re-pauses: 1.1 s) | supported, parent stays paused | refused (adapter path: 11 s) |

On all three, processes survive fork, pause, snapshot restore and five levels of fork depth, and
children share IP, hostname and (unless the hook reseeds it) PRNG state with the parent. **E2B was
the clear winner** and ran every benchmark. Tensorlake's one advantage is forking a suspended
checkpoint without waking it. Live spikes found and fixed several adapter bugs (Morph exec quoting
and base-snapshot deletion, Tensorlake name collisions on repeat copies and leaked server
snapshots). Details: `bench/phase0-*-results.md` in the project files.

## 3. Methods

**Conditions.** Every condition on a task got the same model, effort (medium), step limit,
evaluator and a hard $ cap per run, shared by all branches.

- `linear`: plain tools, no fork, no checkpoints.
- `best_of_n`: the orchestrator forks the seeded sandbox 3 ways, three linear agents run in
  parallel, the evaluator keeps one.
- `fork`: fork tool plus checkpoint/rewind, with auto-checkpoints before risky commands. The agent
  decides whether to fork.
- `forced_fork` (Phase 5 on): like `fork`, but the agent's first state-changing call is held and it
  must fork into 2 to 3 materially different approaches.
- `linear_careful` (Phase 7): `linear` plus the risk warning from the rewind prompt, but no tools.
  This separates the effect of the tool from the effect of the prompt.

**Tasks.**

- Our own stateful tasks: Postgres money-column migration (`billing_migration`), file-moving
  migration with a case-folding collision, a dependency upgrade that rewrites its store on first
  open, a live in-memory session service, and two runtime-only damage tasks (Phase 7).
- SWE-bench Lite (3 Flask instances) and SWE-bench Verified "1-4 hours" instances (Django, SymPy,
  pytest, pylint), validated with the gold patches. The agent sees only the issue; the hidden
  tests run after the run ends.

**Measures.** Solve rate (hidden tests where they exist), $ per solve from token usage at list
prices, wall clock, forks and rewinds, and "dirty end state": a post-run check for records whose
original value no longer exists anywhere on the machine.

**Models.** Opus 5.5 for most runs; Haiku 5.5 as the weaker, cheaper arm in Phases 4 and 7.

## 4. Results

### Phase 1 to 3: demos (`examples/`)

- `count_primes`: prompted to fork, Opus split into trial division and a sieve (fork 1.2 s); the
  evaluator promoted the sieve on latency (10.7 ms vs 6.6 s).
- `billing_migration`: when the task said "deploy the reviewed migration", Opus, Sonnet and Haiku
  5.5 all caught the planted cast bug by reading the code, so nothing needed undoing. Only when
  the task was framed as a rehearsal ("run it as committed first") did Opus break the data and
  rewind (3/3, rewind 2.3 to 2.9 s with Postgres still running). Without the tools Opus took a
  `pg_dump` and restored it. This pattern repeated through every later phase.
- Evaluator: on prepared candidates it rejected the test-editing and regressing ones and, on a
  near-tie, the judge consistently preferred the migration with a pre-drop check.

### Phase 4: equal-budget benchmark, 7 tasks, 3 repeats

| model | condition | solved | $ / solve | dirty end | agent forks |
|---|---|---|---|---|---|
| Opus 5.5 | linear | 18/21 | 0.091 | 0/9 | n/a |
| Opus 5.5 | best_of_n | 18/21 | 0.315 (3.4x) | 0/9 | n/a |
| Opus 5.5 | fork | 18/21 | 0.097 | 0/9 | 0 |
| Haiku 5.5 | linear | 16/21 | 0.005 | 0/9 | n/a |
| Haiku 5.5 | best_of_n | 16/21 | 0.015 | 0/9 | n/a |
| Haiku 5.5 | fork | 15/21 | 0.006 | 0/9 | 0 |

Every condition solved the same tasks. The agents never forked unprompted (0/42). No run ended
dirty, because linear agents made their own backups (`pg_dump`, `cp -a`) and fork agents used
checkpoints instead. Best-of-3 bought nothing and, with Haiku on one Flask task, the evaluator
kept a branch that passed the visible tests but failed the hidden ones.

### Phase 5: harder tasks and forced fork, Opus, 8 tasks, 3 repeats

| condition | solved | $ / solve | forks |
|---|---|---|---|
| linear | 7/24 | 0.40 | n/a |
| best_of_n | 11/24 | 0.81 (2.0x) | n/a |
| fork | 8/24 | 0.35 | 0 unprompted |
| forced_fork | 9/24 | 0.70 (1.7x) | 24 |

The gain is almost entirely one task, `sympy-17630`, where about one attempt in three finds a
2-line root-cause fix and the evaluator reliably picks it. Without it, all conditions are within
one solve. 11 vs 7 of 24 is p ≈ 0.4. Four tasks were 0/12 in every condition: their hidden tests
pin behaviour the issue never states. Forced fork always produced real alternatives (for example
"postprocessor" vs "blockmul") at about 70% of best-of-3's cost. Opus avoided both traps in the
live-process migration task by reading the code, in every condition.

### Phase 6: better selection (PR #12), Opus, 7 tasks, 3 repeats

A writer agent generated reproduction checks from each issue to rank branches. Every working
branch passed every generated check, so **the checks decided 0 of 42 selections**, and solves
were identical to the default selector on the same branches (best-of-3 8/21, forced fork 7/21).
A per-branch hidden-test oracle showed why: with perfect selection, best-of-3 would reach only
10/21 and forced fork 9/21. In 11 of 21 best-of-3 runs no branch was correct. **Generating a
correct candidate is the bottleneck, not choosing one.** The writer added about $0.10 per run for
no gain.

### Phase 7: damage that only shows at runtime (PR #13), 2 tasks, 5 repeats

Two tasks where reading can't reveal the bug and the state can't be copied: a closed-source
time-series tool that reads `--retention` as hours while its help says days (disk too full to
copy the 2.7 GiB store), and a closed-source in-memory session daemon that frees about 15 of
every 16 sessions on a large resize and has no dump command. A careful agent can still rehearse
on a scratch copy.

| model | condition | clean solves | dirty end | runs with a rewind | $ / clean solve |
|---|---|---|---|---|---|
| Haiku 5.5 | linear | 0/10 | 10/10 | 0 | n/a |
| Haiku 5.5 | linear_careful | 0/10 | 10/10 | 0 | n/a |
| Haiku 5.5 | fork | **6/10** | **0/10** | 10 | 0.011 |
| Opus 5.5 | linear | 6/10 | 4/10 | 0 | 0.86 |
| Opus 5.5 | linear_careful (sessiond only) | 3/5 | 2/5 | 0 | 0.76 |
| Opus 5.5 | fork | 10/10 | 0/10 | 0 | 0.11 |

- **Haiku:** ran the runbook command as written in all 30 runs. With rewind it noticed the damage,
  rewound (1.3 to 4.9 s), wrote a lesson, and then either rehearsed and finished (6) or stopped
  safely and asked (4). The warning prompt alone changed nothing. Fisher exact: clean solves
  p ≈ 0.01, dirty end p < 0.0001.
- **Opus:** rewound 0 times. In the fork condition it rehearsed on a scratch copy every time
  (including hardlink copies with `cp -al`). The prompt-only control recovered most of the gap
  on the daemon task, so Opus's gain came mostly from the prompt making it careful, not from
  rewinding.
- **The costly failure mode:** every dirty Opus linear run burned its whole $1 cap, about 10x a
  clean run, on heap forensics through `/proc/<pid>/mem`, and twice started padding the daemon
  with fake sessions to pass the test. None recovered the data. With rewind the same mistake cost
  about 2 s and $0.01.

## 5. Costs

| item | $ (token usage at list prices) |
|---|---|
| Phase 4 (126 runs, Opus + Haiku) | 9.45 |
| Phase 5 (96 runs + 47 screening runs) | 26.3 |
| Phase 6 (42 runs) | 20.2 |
| Phase 7 (59 runs incl. pilot and controls) | 10.05 |
| **benchmarks total** | **≈ 66** |

Demos and provider spikes were a few dollars on top. Sandbox costs were small; every run report
shows 0 leftover sandboxes.

Per-solve cost of the strategies, relative to linear: best-of-3 2.0x to 3.4x, forced fork 1.7x,
agent `fork` about 1x (because it never forked). Checkpoints themselves are nearly free (1 to 2 s,
about 5 s for a 2.7 GiB store).

## 6. Caveats

- **Small samples.** 3 to 5 repeats per cell. Single tasks (`sympy-17630`) dominate totals, and
  most differences of one or two solves are noise. The Phase 6 null result is solid because both
  selectors were compared on identical branches.
- **Task selection.** Phase 5 tasks were chosen because linear Opus failed them at least once,
  which biases linear's absolute rate down. Phase 7 tasks were deliberately built to favour rewind:
  opaque binaries, planted bugs and wrong runbooks.
- **Prompt confound.** Tool descriptions change behaviour even when the tool is never used.
  Phase 7 is the only phase with a prompt-only control.
- **Costs are computed**, from token usage at list prices, not from billing.
- **One provider for benchmarks** (E2B) and two model families (Opus 5.5, Haiku 5.5); Sonnet only
  in the first demo.

## 7. Conclusions

1. **Agents don't use forking on their own.** 0 unprompted forks in 86 runs across Phases 4, 5
   and 7. If forking is wanted, the orchestrator has to impose it (forced fork works and produces
   real alternatives).
2. **Parallel search doesn't pay at equal budget on these tasks.** Gains were within noise and
   concentrated on one task type: several plausible fixes, identical visible-test results, and a
   usable quality signal (here, a smaller root-cause diff) to choose between them.
3. **Selection isn't the lever.** Failures mostly come from no branch being correct, often because
   hidden tests encode requirements the issue never states. More samples of the same reading don't
   find them.
4. **Strong models are careful by default.** Opus caught every bug visible by reading, and backed
   up or rehearsed before risky steps without being told. That removes most of the need for both
   forking and rewind.
5. **Checkpoint/rewind is worth keeping as a safety feature**, not a performance feature. It pays
   when the damage can't be predicted by reading, the state can't be copied, and the agent doesn't
   rehearse on its own. Then it turns permanent data loss into a two-second undo, and it makes a
   cheap model safe: Haiku with rewind matched Opus linear's clean-solve count at about 1/80 of the
   cost per solve on these tasks.
6. **Any agent-tool comparison needs a prompt-only control.**

If this work is picked up again, the cases worth exploring are rewind as a default guardrail for
cheaper models on real operational tasks, and measuring how often agents in real workloads hit
opaque irreversible operations. Further benchmarking of forking for search is unlikely to change
the picture.

## Sources

Per-phase write-ups and raw run data (every run's report and transcript) are in the project files
under `bench/`: `phase0-e2b-results.md`, `phase0-morph-tensorlake-results.md`,
`phase0-tensorlake-results.md`, `evaluator-results.md`, `rewind-demo-results.md`,
`phase4-results.md`, `phase5-results.md`, `phase6-results.md`, `phase7-results.md`, and the
`phase4/` to `phase7/` folders. The original design is [PLAN.md](PLAN.md). Code changes are PRs
#1 to #13 in this repository.
