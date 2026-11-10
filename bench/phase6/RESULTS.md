# Phase 6 results: a reproduction-test selector (2026-11-10, E2B, Opus 5.5)

**Short answer:** generated reproduction tests didn't change a single selection. On the 7 Phase 5 SWE-bench tasks, the new selector solved exactly as many runs as the old one on the same branches: best-of-3 8/21 and forced fork 7/21 for both. In every fork point that had working branches, all branches passed every generated check, so the checks never separated them. When at least one branch passed the hidden tests, both selectors picked a passing branch 15 of 19 times. The writer added about $0.10 per run, which raised $/solve by 24% for best-of-3 and 36% for forced fork, for no gain.

The more useful finding is about where the headroom is. With perfect selection, best-of-3 would have solved 10/21 and forced fork 9/21: only 2 more solves each. In 11 of 21 best-of-3 runs, no branch passed the hidden tests at all. On this task set, generating a correct candidate is the bottleneck, not choosing one.

Code: `sfork/eval/repro.py`, `ExploreConfig.selector="repro"` in `sfork/orchestrator/explore.py`, bench conditions `best_of_n_repro` / `forced_fork_repro`, and this folder (suite, analysis script). Raw data (summaries and every run report with transcripts) is kept outside the repo; rerun with `run.py` to reproduce.

## What I built

- **A writer agent at each fork point.** While the branches work, a separate Opus agent gets the task statement in a throwaway copy of the fork point. It writes executable checks for the behaviour the issue asks for, plus a couple of guard checks, without fixing anything. It is told to cover closely related cases a maintainer's tests would likely include, but nothing the issue leaves open. The checks go in `/tmp/sfork_repro/run.sh` and print `REPRO <name> PASS|FAIL`. The writer must run them at the fork point and confirm the issue checks fail there. Its cap was $0.30, metered separately so it never ate into the branches' cap.
- **Ranking.** After the branches finish, the checks run in every branch and at the fork point. Branches are ranked by checks passed, right after the gates and ahead of visible tests and score, and the LLM judge still breaks near-ties.
- **Paired measurement.** Each run also records what the default selector would have picked on the same branches. The hidden tests also run in every branch before losers are deleted; this is recorded only, never used to select. So each run compares the two selectors on identical branches and also shows whether any branch was correct.

## Setup

- These are the 7 SWE-bench Verified tasks from the Phase 5 main runs (the live-migration task was left out because it has no hidden tests), with the same $ caps, model (Opus 5.5, effort medium) and 50-step limit.
- Runs: best-of-3 and forced fork, each with the repro selector, × 3 repeats = 42 runs.
- Linear was not rerun. The comparison below uses Phase 5's linear runs on the same tasks.

## Results

| condition | solved | default selector, same branches | some branch correct | $ total | writer $ | $ / solve | $ / solve without writer |
|---|---|---|---|---|---|---|---|
| best_of_n + repro | 8/21 | 8/21 | 10/21 | 11.91 | 2.27 | 1.49 | 1.21 |
| forced_fork + repro | 7/21 | 7/21 | 9/21 | 8.19 | 2.15 | 1.17 | 0.86 |

The same tasks in Phase 5, with the default selector (independent runs):

| condition | solved | $ / solve |
|---|---|---|
| linear | 4/21 | 0.64 |
| best_of_n | 8/21 | 1.01 |
| fork | 5/21 | 0.50 |
| forced_fork | 6/21 | 0.93 |

### Selection accuracy

| | runs where some branch was correct | repro selector picked a correct branch | default selector did |
|---|---|---|---|
| best_of_n | 10 | 8 | 8 |
| forced_fork | 9 | 7 | 7 |

- **The repro signal decided 0 of 42 selections.** Writers produced 5 to 8 checks per fork point, and usually 3 to 6 of them failed at the fork point, as intended. Every branch passed all of them, with one exception: a best-of-3 branch on sympy-13852 broke the module and scored 0/6, and it would have lost anyway.
- The picks differed in 5 runs, but only because the judge's answer isn't stable between calls (once, a transient API error made it fail). None of those 5 changed whether the run was solved.

### Why the checks didn't separate branches

The writer tests the issue as stated, and so does every branch: each agent reproduces the reported failure and fixes it. In all four missed selections, the hidden tests pin down something the issue doesn't say. (sympy-17630 is the same story: the hidden test expects `Add(ZeroMatrix, ZeroMatrix) == ZeroMatrix`, which no writer checked. But there, diff size and the judge picked the root-cause branch every time one existed, 4 of 4.)

- **pytest-10356 (forced fork).** The hidden test calls `get_unpacked_marks(C)` and expects a list in MRO order. The branch that fixed it in the collector did the same at the user level, so the writer's "both marks apply" checks passed, but it returned a generator.
- **sympy-13852 (best-of-3 rep 3, forced fork rep 1).** The hidden test expects `polylog(2, 2) == pi**2/4 - I*pi*log(2)`. Only one branch added it, and it was the larger diff (29 lines vs 8), so diff size picked against it.
- **sympy-16597 (best-of-3 rep 3).** The correct branch made `Symbol(integer=True).is_finite` True. That breaks three existing visible tests, which the hidden test patch updates. So the right branch scored lower on visible tests (76/79 vs 77/79) and lost.

So in these cases the existing signals sometimes point the wrong way (visible tests that the fix legitimately changes, a diff-size bonus for the narrower fix). Issue-derived tests can't fix that, because the deciding behaviour isn't in the issue.

## Takeaways

1. **Issue-level reproduction tests carry no selection signal for a strong agent.** Opus branches all pass them, because reproducing the issue is the first thing each branch does. This kind of check would only help with weaker models, or with branches that fail in ways that show up at the level of the issue.
2. **Selection headroom is small here.** Perfect selection would add 2 solves per condition (best-of-3 8→10, forced fork 7→9). Most failures come from no branch being correct, so diversity of generation matters more than selection on these tasks.
3. **What could recover the remaining 4 misses:**
   - Turn down diff size and visible-test penalties when branches differ at the root cause.
   - Cross-run each branch's own tests on the others. Branches that change visible tests, or add edge cases like `polylog(2, 2)`, encode exactly the extra behaviour the hidden tests check.

   Neither is worth much on this set (at most +2 solves per condition), so I'd only pursue them on a task set with more headroom.
4. **The writer isn't worth its cost as a default.** I left it behind `selector: repro` (off by default) rather than removing it, since the paired-oracle harness (`branch_checks`) is useful for any future selector experiment.

## Spend

| item | $ |
|---|---|
| main runs (42, including the 4 pilot runs reused as rep 1) | 20.10 |
| discarded run (pytest-10356 forced fork rep 3: Anthropic API "credit balance too low" mid-run, rerun) and an interrupted rerun | ≈ 0.10 |
| total | **≈ 20.2** |

## Caveats

- 3 repeats, 7 tasks. Since the two selectors are compared on the same branches, the null result is solid for these tasks (0 of 42 decided by repro). The solve counts against Phase 5 are independent samples and noisy.
- The Anthropic API briefly returned "credit balance is too low" around 17:45Z. It hit one run's branches (rerun) and the judge calls in two others (django-15268 forced fork rep 3, pytest-10356 best-of-3 rep 3). Those two selections then fell back to diff size / the other judge order. Both solved and the default pick was the same branch.
- One django-15268 run failed setup on a TLS error during `git clone` and was rerun.

