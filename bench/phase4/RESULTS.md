# Phase 4 results: linear vs best-of-N vs agent-initiated forking (2026-11-07, E2B)

**Short answer:** on these 7 tasks, forking didn't pay for itself. All three conditions solved the same number of tasks with Opus 5.5 (18/21). Best-of-3 cost 3.4x as much per solve and ran about 40% longer. The fork condition cost about the same as linear (+7% $/solve with Opus, +29% with Haiku, which also solved one fewer). The agents never called `fork` unprompted (0 times in 42 runs), so all of that condition's effect came from checkpoint/rewind. No run in any condition ended with damaged state, because the linear agents made their own backups (pg_dump, `cp -a`) before every risky step. So the benchmark counted **0 dirty-state failures avoided**. Rewind replaced those manual backups but didn't fix failures that would otherwise have happened.

Harness: this folder. Raw data (summaries and every run report with transcripts) is kept outside the repo; rerun with `run.py` to reproduce.

## Setup

- **Conditions.** Every condition on a task gets the same hard $ cap per run, the same model and effort (medium), a limit of 40 turns per branch, and the same evaluator.
  - **linear:** plain tools only (no fork, no checkpoint/rewind).
  - **best_of_n:** the orchestrator forks the seeded sandbox 3 ways; 3 linear agents run in parallel and share the cap; the evaluator (with the LLM judge on near-ties) keeps one.
  - **fork:** the fork tool plus checkpoint/rewind, with auto-checkpoints before risky commands.
- **Cap.** About 3x a linear Opus pilot run: $0.15 to $0.45 per run, scaled by 0.1 for the Haiku arm. The cap is enforced before each model call.
- **Tasks.**
  - Stateful migrations: `billing_migration` (Postgres rehearsal) and a new `attachments_migration` (a file-moving migration whose case-folding overwrites files).
  - Dependency upgrade: a new `notes_dep_upgrade` (the 2.0 library rewrites the store on first open and corrupts non-ASCII text unless told the legacy encoding; the 1.x wheel is gone).
  - Design choice: `count_primes`, with a neutral prompt and a 400 ms bar.
  - SWE-bench Lite: `pallets__flask-4045`, `-4992`, `-5063`. The agent sees only the issue. The hidden FAIL_TO_PASS + PASS_TO_PASS tests run after the run ends. All three were validated with the gold patches.
- **Dirty state.** A check script runs on the final head and counts records whose original value no longer exists anywhere on the machine.
- **Repeats.** 3 per (task, condition). That is 63 Opus runs and 63 Haiku runs.

## Opus 5.5 (main arm)

| condition | solved | $ total | $ / solve | median wall | dirty end state | rewinds | agent forks |
|---|---|---|---|---|---|---|---|
| linear | 18/21 | 1.64 | 0.091 | 35 s | 0/9 | 0 | n/a |
| best_of_n (3) | 18/21 | 5.66 | 0.315 | 49 s | 0/9 | 0 | n/a |
| fork | 18/21 | 1.74 | 0.097 | 33 s | 0/9 | 3 | 0 |

- **Same solves.** Every condition solved the same tasks. All 9 misses are `flask-4045`, in every condition: the agents made blueprint names with dots raise `ValueError` but left the old `assert` on dotted endpoints, which the hidden test expects to raise `ValueError` too. Best-of-3 can't fix this, because all three samples make the same choice and the visible tests can't tell them apart.
- **Billing.** The rehearsal forces a damaging step. The fork agents rewound in 3/3 runs (8 checkpoints in total). The linear agents took a `pg_dump` and restored it in 3/3 runs. Cost was $0.124 vs $0.115 per solve, and median wall time 84 s vs 69 s.
- **Attachments and notes.** Opus usually read the code or the changelog first and fixed the bug before running anything. With the fork tools it checkpointed instead of copying data (1 manual backup in 6 fork runs, vs 6 in 6 linear runs).
- **Best-of-3** hit the cap twice on `count_primes` (cap $0.15), which shows the cap is binding. Its $ per solve was 2.9x to 4.1x linear's, depending on the task.

## Haiku 5.5 (cheap, weaker arm)

| condition | solved | $ total | $ / solve | median wall | dirty end state | rewinds | agent forks |
|---|---|---|---|---|---|---|---|
| linear | 16/21 | 0.08 | 0.005 | 30 s | 0/9 | 0 | n/a |
| best_of_n (3) | 16/21 | 0.25 | 0.015 | 39 s | 0/9 | 0 | n/a |
| fork | 15/21 | 0.09 | 0.006 | 33 s | 0/9 | 5 | 0 |

- **Best-of-3 helped and hurt.** It helped on `attachments_migration` (3/3 vs 2/3 linear). It hurt on `flask-5063` (1/3 vs 2/3), where the evaluator can only rank on the visible tests and diff size, and it kept a branch that failed the hidden tests. That's the weak point of best-of-N without an oracle.
- **Rewinds.** Haiku rewound 5 times (3 billing, 2 attachments). It still made manual backups in 4 of 6 fork runs on the non-billing tasks.

## What this means for the plan

1. **Agents don't fork on their own.** In 42 runs where forking was available and the prompt didn't mention it, neither model used it once. Agent-initiated forking needs either prompting (the original `count_primes` demo prompt asks for it) or a policy that proposes forks at decision points. PLAN.md section 10 lists "agents fork too much or too little" as a risk to tune here. The measured answer is "never".
2. **Checkpoint/rewind is about cost-neutral and replaces ad hoc backups.** It cost about the same as linear here. It doesn't yet show an advantage, because careful agents back up small state themselves. The case for it is state that can't be backed up cheaply (large databases, running processes, installed system packages), which these tasks don't have.
3. **Best-of-N at equal $ buys nothing on tasks the model mostly solves.** It needs a selection signal better than the visible tests. On SWE-style tasks the judge picked a wrong branch.
4. **Next experiments that could show a difference:** tasks where linear solve rate is low but variable (harder SWE-bench instances, or stateful tasks with big or un-dumpable state), and a "forced fork" condition that forks at the first decision point. The current tasks are close to saturated for Opus.

## Spend

| item | $ |
|---|---|
| Opus 5.5 (63 runs incl. 7 pilot) | 9.04 |
| Haiku 5.5 (63 runs) | 0.41 |
| total | **≈ 9.45** |

E2B sandboxes were cleaned up: 0 leftover in every run report, and `Sandbox.list()` was empty afterwards.

## Caveats

- 3 repeats per cell is small. Differences of one solve are noise.
- Wall clock includes setup (apt-get Postgres for billing, git clone + pip for Flask) and runs at concurrency 2.
- The 7 Opus pilot linear runs (rep 1) ran under a $1.20 cap. None came near it (max $0.134), so they're comparable with the later runs.
- Costs are computed from token usage at list prices, not from billing.
