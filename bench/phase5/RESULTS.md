# Phase 5 results: harder tasks and a forced-fork condition (2026-11-09, E2B, Opus 5.5)

**Short answer:** on tasks Opus fails at least sometimes, parallel attempts finally solved more than a single agent did. Best-of-3 solved 11 of 24 runs and linear solved 7 of 24. Forced fork solved 9/24. Agent-initiated fork solved 8/24. Almost all of that gain comes from one task, `sympy__sympy-17630`. There, about one attempt in three finds the 2-line root-cause fix, and the selector reliably picks it (best-of-3 3/3, forced fork 2/3, linear 0/3). Without that task, all four conditions are within one solve of each other. With 3 repeats that difference is noise (11 vs 7 out of 24 has p ≈ 0.4). The extra solves cost 2.0x linear's $/solve for best-of-3 and 1.7x for forced fork. Agents still never forked on their own: 0 times in 24 `fork` runs, the same as Phase 4. The new live-process stateful task didn't separate the conditions. Opus spotted both traps by reading the code, in every condition, and no run ended with lost state.

Harness: this folder (suite, tasks, README) on top of `bench/phase4/run.py`. Raw data (summaries and every run report with transcripts) is kept outside the repo; rerun with `run.py` to reproduce.

## What's new

- **Harder tasks.** These are SWE-bench Verified instances that its annotators rated "1-4 hours". I set up all 34 such instances from Django, SymPy, pytest and pylint. 32 validated on E2B: the hidden tests fail before the fix and pass with the gold patch. Two Django ones failed setup and were dropped. Older releases run on a uv-installed Python 3.9. I screened them with linear Opus runs (1–2 each, $5.50). Opus solved 36 of 47 screening runs, and most "1-4 hour" instances on the first try. I kept the 7 instances it failed at least once.
- **`sessions_live_migration`** (stateful, un-dumpable). A session service runs as a process with simulated users logging in. Its sessions exist only in that process's memory, and the export they came from is gone. The agent must convert user ids to ints in the live process and make new logins store ints, without a restart. It has two traps, and both lose data for good:
  - The teammate's migration maps legacy `L-` ids onto other users.
  - `/admin/reload` of `app.sessions` re-runs `STORE = {}`.

  Validation: running the migration as written loses 1,218 of 6,000 sessions, and the naive reload loses all 6,000.
- **`forced_fork` condition.** It works like `fork`, but the root agent's first state-changing call is held back and the agent is told to fork into 2–3 materially different approaches. "State-changing" means file writes outside /tmp, package installs, DB clients, kills and migrations. If the agent still won't fork after two prompts, the orchestrator forks it into generic plans.

## Setup

- 8 tasks × 4 conditions × 3 repeats = 96 runs. All runs used Opus 5.5 at effort medium, with a limit of 50 turns per branch.
- **Equal $ cap per run on each task, shared by all branches.** As in Phase 4, the cap is about 3x the most expensive linear pilot run (at least $0.30): $0.30 to $0.40, and $1.10 for `django-16263`.
- These were fresh runs, not the screening runs. Still, tasks were picked because linear failed them, so linear's absolute rate here is biased low against itself. Compare conditions with each other, not with Phase 4.

## Results

| condition | solved | $ total | mean $ / run | $ / solve | median wall | forks | dirty end state |
|---|---|---|---|---|---|---|---|
| linear | 7/24 | 2.83 | 0.118 | 0.404 | 103 s | n/a | 0/3 |
| best_of_n (3) | **11/24** | 8.95 | 0.373 | 0.814 | 155 s | n/a | 0/3 |
| fork | 8/24 | 2.76 | 0.115 | 0.345 | 102 s | **0** | 0/3 |
| forced_fork | 9/24 | 6.27 | 0.261 | 0.696 | 143 s | 24 | 0/3 |

Solved per task (out of 3):

| task | linear | best_of_n | fork | forced_fork |
|---|---|---|---|---|
| sessions_live_migration | 3 | 3 | 3 | 3 |
| sympy__sympy-17630 | 0 | **3** | 0 | **2** |
| django__django-15268 | 2 | 3 | 2 | 2 |
| pytest-dev__pytest-10356 | 2 | 2 | 3 | 2 |
| django__django-16263 | 0 | 0 | 0 | 0 |
| sympy__sympy-18199 | 0 | 0 | 0 | 0 |
| sympy__sympy-16597 | 0 | 0 | 0 | 0 |
| sympy__sympy-13852 | 0 | 0 | 0 | 0 |

### Where parallel attempts helped: sympy-17630

The issue is that multiplying a BlockMatrix containing ZeroMatrix blocks crashes. Linear and `fork` agents patch the symptom in `blockmatrix.py` or `dense.py` (6–8 lines). The hidden test also expects `Add(ZeroMatrix, ZeroMatrix)` to stay a ZeroMatrix, and only a fix at the root cause in `matexpr.py`'s postprocessor (2 lines) gives that.

Every branch passes the visible tests, so selection comes down to diff size and the LLM judge. Both prefer the 2-line root-cause fix whenever some branch found it.

- **Best-of-3:** in each run, one or two of the three samples found it, and it won 3/3.
- **Forced fork:** the agent named it as an explicit alternative ("postprocessor" vs "blockmul") in 2 of 3 runs, and won both. In the third run it forked into two blockmatrix-level variants and missed.

This is the case forking is meant for: the approaches are materially different, the visible tests can't tell them apart, and a quality signal (here a smaller diff at the root cause) can.

### Where nothing helped

Four tasks went 0 for 12 in every condition. In each, the hidden tests pin down behaviour the issue doesn't state:

- `16263`: one more aliased-aggregate pruning case.
- `13852`: specific closed forms such as `polylog(2, 2)`.
- `18199`: a `solveset` result the issue never mentions.
- `16597`: `is_finite` on `Symbol(infinite=…)` and other assumptions.

More samples of the same reading don't find an unstated requirement. Forking only helps when some branch actually explores the other reading.

### Forced fork

- **It always fired.** 23 of 24 runs hit the decision point and forked at the first prompt; the generic fallback was never needed. One run forked on its own before its first change, because the system prompt says it will be asked to.
- **The alternatives were real.** 22 runs forked 2 ways and 2 runs 3 ways. Examples:
  - `guarded-reload` vs `migration-patches-live` (sessions)
  - `postprocessor` vs `blockmul` (17630)
- **It's cheaper than best-of-3** (mean $0.26 vs $0.37 per run), because the branches share the exploration prefix. It found the 17630 root cause less reliably than three independent samples (2/3 vs 3/3).
- **Selection was mostly by the judge.** Branches usually tied on visible tests, so the LLM judge decided 14 of 24 forced-fork selections and 13 of 24 best-of-3 ones.

### Agent-initiated forking and rewind

- **Agents still don't fork on their own.** Unprompted forks are 0 in 24 runs here, and 0 in 66 across Phases 4 and 5. Prompting at a decision point is what makes forking happen.
- **No rewinds anywhere.** No agent needed a rewind; the auto-checkpoints were taken and never used.

### The live-process task

All 12 runs solved it with no lost sessions. Every agent read `app/users.py`, saw the legacy `L-` offset, fixed the migration first, and either guarded `STORE` against reload or hot-patched `new_session` from inside the migration. So, as in Phase 4, a careful model doesn't trip over traps that are visible in the code. Un-dumpable state only matters once a mistake happens, and Opus didn't make one.

## Takeaways

1. Harder tasks finally separated the conditions, but only on the task type forking is built for: several plausible fixes, the same visible-test result, and a usable quality signal to choose between them. The gain was +4 solves for best-of-3 at 2x $/solve, and +2 for forced fork at 1.7x.
2. Forced fork is the practical way to get agent forking. Left to themselves, agents never fork. When forced at the first change, they propose sensible, distinct approaches at about 70% of best-of-3's cost.
3. Selection is now the bottleneck. On 4 of 8 tasks no branch was right, and on SWE tasks the visible tests rarely separate branches, so the judge and diff size decide. Better selection signals would be the next lever, such as generated tests or a reproduction of the issue that each branch must pass.
4. Reading-detectable traps don't trip Opus. A stateful task that separates checkpoint/rewind from linear needs damage that only shows up at runtime, or a weaker model.

## Spend

| item | $ |
|---|---|
| screening (47 linear runs) | 5.50 |
| main runs (96) | 20.81 |
| total | **≈ 26.3** |

E2B sandboxes were cleaned up: 0 leftover in every run report, and `Sandbox.list()` was empty afterwards.

## Caveats

- 3 repeats per cell, 8 tasks. Single-task effects, like 17630, dominate the totals.
- Tasks were selected on linear failures. Fresh runs reduce the bias but don't remove it.
- Wall clock includes setup (git clone, pip, uv Python) and runs at concurrency 3. SymPy test runs are slow (2–10 min per run).
- Costs are computed from token usage at list prices, not from billing.
