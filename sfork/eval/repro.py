"""Reproduction checks written from the task at selection time, run in every branch.

On SWE-style tasks the visible tests rarely tell branches apart (Phase 5: most selections came down
to diff size and the LLM judge). This adds a signal that targets the task itself: while the branches
work, a separate *writer* agent gets the task statement in a throwaway copy of the fork point and
writes executable checks for the behaviour the task asks for, without fixing anything. After the
branches finish, the same checks run in every branch and at the fork point, and the selector ranks
branches by how many checks pass, right after the gates and ahead of the visible tests and score
(`sfork.eval.scoring.Signal`). The judge still breaks near-ties among branches that pass the same
checks.

Protocol: the writer leaves everything in REPRO_DIR, with an entry point REPRO_DIR/run.sh. Run from
the project directory, it prints one line per check:

    REPRO <name> PASS
    REPRO <name> FAIL

A check that crashes counts as FAIL, as does a check that printed nothing in a branch but did at the
fork point (e.g. the branch broke the import). Checks that already pass at the fork point are kept:
they guard behaviour the fix must not break.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .scoring import Signal

REPRO_DIR = "/tmp/sfork_repro"
_LINE = re.compile(r"^\s*REPRO\s+(\S+)\s+(PASS|FAIL)\b", re.M)
MAX_BUNDLE_BYTES = 400_000

WRITER_SYSTEM = """\
You write reproduction checks for a software task, inside a Linux sandbox, through tools. The \
project is in {workdir} and commands run from there. You do NOT fix anything: other engineers are \
fixing the task in their own copies of this machine, and your checks will be run in each of their \
copies to decide whose fix is right.

Write checks for the behaviour the task asks for:
- the exact failure the task describes, turned into assertions about the correct behaviour;
- the closely related cases a maintainer's tests for this fix would most likely cover (other \
inputs, types or code paths the same fix has to handle), but nothing the task leaves open to \
interpretation;
- one or two checks that behaviour the task depends on and that works today keeps working.
Assert on observable results (return values, types, raised exceptions, output), not on how the \
fix is implemented, so any correct fix passes. Prefer 3-8 focused checks.

Put everything in {repro_dir}/ (outside the project, so it is not part of anyone's change). The \
entry point is {repro_dir}/run.sh; it is run as `bash {repro_dir}/run.sh` from the project \
directory, with the same interpreter and environment the project's tests use. It must print one \
line per check, exactly `REPRO <name> PASS` or `REPRO <name> FAIL` (name without spaces), run each \
check in isolation (e.g. its own subprocess, with a timeout of at most 60 s) so that one crash or \
hang doesn't hide the others, never modify files in the project, and finish within 5 minutes.

Run it here before you finish. This copy has no fix, so checks for the reported problem should \
print FAIL here (for the right reason: an assertion about the behaviour, not a typo or import error \
in your check) and the guard checks PASS. Then call done with a one-line description of each check."""

WRITER_PROMPT = """\
Here is the task the other engineers were given. Write the reproduction checks for it.

<task>
{task}
</task>"""


def parse(output: str) -> dict[str, bool]:
    """Check name -> passed, from run.sh output (the last line for a name wins)."""
    return {m.group(1): m.group(2) == "PASS" for m in _LINE.finditer(output or "")}


@dataclass
class ReproResult:
    """What the writer produced and how every branch did on it."""

    ok: bool = False
    reason: str = ""
    summary: str = ""
    bundle_b64: str = ""
    bundle_bytes: int = 0
    files: list[str] = field(default_factory=list)
    run_sh: str = ""
    fork_point: dict[str, bool] = field(default_factory=dict)
    fork_point_output: str = ""
    branches: dict[str, dict[str, bool]] = field(default_factory=dict)  # node id -> check -> passed
    outputs: dict[str, str] = field(default_factory=dict)  # node id -> tail of run.sh output
    labels: dict[str, str] = field(default_factory=dict)  # node id -> branch name
    writer_cost_usd: float = 0.0
    writer_steps: int = 0
    writer_seconds: float = 0.0
    run_seconds: float = 0.0
    transcript: list[Any] | None = None

    @property
    def checks(self) -> list[str]:
        """Every check seen at the fork point or in any branch."""
        names = dict.fromkeys(self.fork_point)
        for res in self.branches.values():
            names.update(dict.fromkeys(res))
        return list(names)

    def passed(self, node_id: str) -> int:
        res = self.branches.get(node_id, {})
        return sum(bool(res.get(c)) for c in self.checks)

    def signal(self) -> Signal | None:
        """None when there is nothing usable (no checks, or the writer failed)."""
        checks = self.checks
        if not self.ok or not checks:
            return None
        n = len(checks)
        return Signal(
            name="repro",
            values={nid: self.passed(nid) / n for nid in self.branches},
            shown={nid: f"{self.passed(nid)}/{n}" for nid in self.branches},
        )

    def as_dict(self) -> dict[str, Any]:
        n = len(self.checks)
        return {
            "ok": self.ok,
            "reason": self.reason,
            "summary": self.summary[:2000],
            "files": self.files,
            "bundle_bytes": self.bundle_bytes,
            "run_sh": self.run_sh[:8000],
            "checks": self.checks,
            "failing_at_fork_point": sum(not v for v in self.fork_point.values()),
            "fork_point": self.fork_point,
            "fork_point_output": self.fork_point_output[-3000:],
            "branches": {
                nid: {
                    "label": self.labels.get(nid),
                    "passed": self.passed(nid),
                    "total": n,
                    "checks": res,
                    "output": self.outputs.get(nid, "")[-3000:],
                }
                for nid, res in self.branches.items()
            },
            "writer_cost_usd": round(self.writer_cost_usd, 5),
            "writer_steps": self.writer_steps,
            "writer_seconds": round(self.writer_seconds, 2),
            "run_seconds": round(self.run_seconds, 2),
        }


__all__ = ["MAX_BUNDLE_BYTES", "REPRO_DIR", "WRITER_PROMPT", "WRITER_SYSTEM", "ReproResult", "parse"]
