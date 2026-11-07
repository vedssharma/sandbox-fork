# Phase 4 benchmark: linear vs best-of-N vs agent-initiated forking

PLAN.md Phase 4 asks whether forking pays for itself. `run.py` runs the same tasks under three
conditions, at the same hard $ cap per run on each task:

| condition | what the agent gets |
|---|---|
| `linear` | one agent, plain tools (bash, read/write file, run_tests, done). No fork, no checkpoint/rewind. |
| `best_of_n` | the orchestrator forks the seeded sandbox N=3 ways before the agent starts; three linear agents run in parallel; the evaluator keeps the best. The cap is shared. |
| `fork` | one agent with the fork tool and checkpoint/rewind, plus automatic checkpoints before risky commands. The cap is shared by all branches. |

Same model, effort, step limit (40 turns per branch) and evaluator in every condition. The cap is
enforced by `sfork.core.cost.BudgetedLLM`: once a run's spend reaches it, no new model call starts
and the branch stops as if it had run out of steps (so a run can end above the cap by at most one
call per running branch). Spend is computed from token usage at list prices.

## Tasks (`suite.yaml`)

| task | kind | what makes it hard |
|---|---|---|
| `billing_migration` (examples/) | stateful migration | Rehearse a reviewed Postgres migration that corrupts amounts >= $10k and drops the source column. |
| `attachments_migration` | stateful migration | A file-moving migration lower-cases names into per-owner folders; same-owner names that differ only in case overwrite each other. |
| `notes_dep_upgrade` | dependency upgrade | Upgrade a library whose 2.0 rewrites the on-disk store the first time it opens it, decoding 1.x's latin-1 values as UTF-8 unless told otherwise. The 1.x wheel is gone. |
| `count_primes` (examples/) | design choice | Trial division vs sieve; solved only under 400 ms. Neutral prompt (the example's prompt tells the agent to fork). |
| `pallets__flask-4045`, `-4992`, `-5063` | SWE-bench Lite | Real issues. The agent sees only the issue and the existing tests; the hidden FAIL_TO_PASS + PASS_TO_PASS tests run after the run ends. |

## Metrics

- **solved**: the final head passes every gate and visible test and isn't disqualified; SWE tasks
  must pass their hidden tests instead of the visible ones; count_primes must also be under 400 ms.
- **wall clock**: from sandbox creation to teardown, setup included.
- **$ per solve**: total spend over solved runs.
- **dirty end state**: for stateful tasks, a check script runs on the final head before teardown
  and counts records whose original value no longer exists anywhere on the machine
  (`checks/*_dirty.py`). That is damage no code change can repair.

## Running

    pip install -e ".[dev]"
    ANTHROPIC_API_KEY=... E2B_API_KEY=... python bench/phase4/run.py --repeats 3 --out bench/results/phase4
    python bench/phase4/run.py --model claude-haiku-5-5 --budget-scale 0.1 --repeats 3 --out bench/results/phase4-haiku

Reruns skip runs that already have a report. `validate.py TASK_DIR` checks a task's setup on E2B
and that its reference fix (`gold.sh`, or the SWE-bench gold patch) passes; `swe.py` regenerates
the SWE task directories from `swe/instances.json`.
