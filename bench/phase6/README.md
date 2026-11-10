# Phase 6 benchmark: a reproduction-test selector

Phase 5 found that on SWE-bench tasks the branches usually tie on the visible tests, so diff size
and the LLM judge pick the winner. Phase 6 tries a sharper signal and measures selection directly.

- **Repro selector** (`ExploreConfig.selector = "repro"`, `sfork/eval/repro.py`). At each fork
  point, while the branches work, a writer agent gets the task in a scratch fork of the fork point
  and writes checks for it (`/tmp/sfork_repro/run.sh`, printing `REPRO <name> PASS|FAIL`) without
  fixing anything. After the branches finish, the checks run in every branch and at the fork point.
  Branches rank by checks passed right after the gates; the judge still breaks near-ties. The
  writer has its own $ cap (`repro_cap_usd`) and is metered apart from the run's cap; reports add
  it as `selection_cost`.
- **Paired oracle.** The repro selector also records the default selector's pick on the same
  branches, and `ExploreConfig.branch_checks` (here the hidden SWE-bench tests) runs in every branch
  before the losers are deleted. That's recorded only, never used to select, so each run shows
  whether a correct branch existed and whether each selector picked one.

Conditions `best_of_n_repro` and `forced_fork_repro` in `bench/phase4/run.py` are `best_of_n` and
`forced_fork` with both of the above.

## Running

    python bench/phase4/run.py --suite bench/phase6/suite.yaml --tasks all \
        --conditions best_of_n_repro,forced_fork_repro --repeats 3 --concurrency 3 --out bench/results/phase6
    python bench/phase6/analyze.py bench/results/phase6 --phase5 <phase5 summary.json>

See RESULTS.md.
