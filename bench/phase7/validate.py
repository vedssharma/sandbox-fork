"""Validate the Phase 7 tasks on E2B: seed a sandbox, fork it, then run the runbook as written
(naive.sh) in one copy and the reference fix (gold.sh) in the other. Expect: naive loses data
(dirty check exits 1, tests fail), gold keeps it (dirty check 0, tests pass).

    python bench/phase7/validate.py bench/phase7/tasks/tsdb_retention bench/phase7/tasks/sessiond_resize
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from e2b import AsyncSandbox  # noqa: E402

from sfork.daytona.base import ExecResult  # noqa: E402
from sfork.eval.evaluator import evaluate, in_dir  # noqa: E402
from sfork.orchestrator.explore import Task, seed_task  # noqa: E402

CHECKS = {"tsdb_retention": "tsdb_dirty.py", "sessiond_resize": "sessiond_dirty.py"}


def runner(sb):
    async def ex(cmd: str, timeout: int) -> ExecResult:
        try:
            r = await sb.commands.run(cmd, user="root", timeout=timeout)
            return ExecResult(r.exit_code, (r.stdout or "") + (r.stderr or ""))
        except Exception as e:
            code = getattr(e, "exit_code", 1)
            return ExecResult(code, (getattr(e, "stdout", "") or "") + (getattr(e, "stderr", "") or str(e)))

    return ex


async def run_script(sb, task: Task, script: Path, label: str) -> None:
    ex = runner(sb)
    await ex(f"cat > /tmp/s.sh <<'SFORK_EOF'\n{script.read_text()}\nSFORK_EOF", 60)
    r = await ex(in_dir(task.eval.workdir, "bash /tmp/s.sh"), 900)
    print(f"[{task.name}/{label}] script exit {r.exit_code}: {r.output[-600:]}", flush=True)
    ev = await evaluate(lambda c, t: ex(c, t), task.eval, node_id="x", label=label)
    print(f"[{task.name}/{label}] eval: {ev.summary()}", flush=True)
    chk = (ROOT / "bench/phase7/checks" / CHECKS[task.name]).read_text()
    r = await ex(f"python3 - <<'SFORK_CHECK'\n{chk}\nSFORK_CHECK", 300)
    print(f"[{task.name}/{label}] dirty check exit {r.exit_code}: {r.output.strip()[-300:]}", flush=True)


async def run_one(path: str) -> None:
    task = Task.load(path)
    sb = await AsyncSandbox.create(timeout=1800)
    forks = []
    t0 = time.monotonic()
    try:
        await seed_task(runner(sb), task)
        print(f"[{task.name}] seeded in {time.monotonic() - t0:.0f}s", flush=True)
        ev = await evaluate(lambda c, t: runner(sb)(c, t), task.eval, node_id="x", label="base")
        print(f"[{task.name}] baseline: {ev.summary()}", flush=True)
        t1 = time.monotonic()
        res = await AsyncSandbox.fork(sb.sandbox_id, timeout=1800, count=1)
        forks.extend(r for r in res if not isinstance(r, Exception))
        other = forks[0]
        print(f"[{task.name}] copied in {time.monotonic() - t1:.1f}s", flush=True)
        await asyncio.gather(
            run_script(sb, task, Path(path) / "naive.sh", "naive"),
            run_script(other, task, Path(path) / "gold.sh", "gold"),
        )
    finally:
        await sb.kill()
        for f in forks:
            await f.kill()


async def main() -> None:
    await asyncio.gather(*(run_one(p) for p in sys.argv[1:]))


if __name__ == "__main__":
    asyncio.run(main())
