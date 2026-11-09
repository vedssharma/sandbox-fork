"""Check that a task's setup works on E2B, and (for SWE tasks) that the hidden check fails before the
fix and passes with the gold patch. Usage: python bench/phase4/validate.py TASK_DIR [...]"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from e2b import AsyncSandbox  # noqa: E402

from bench.phase4.swe import hidden_check, load_instances  # noqa: E402
from sfork.daytona.base import ExecResult  # noqa: E402
from sfork.eval.evaluator import evaluate, in_dir  # noqa: E402
from sfork.orchestrator.explore import Task, seed_task  # noqa: E402


async def run_one(path: str) -> None:
    task = Task.load(path)
    sb = await AsyncSandbox.create(timeout=1800)
    t0 = time.monotonic()

    async def ex(cmd: str, timeout: int) -> ExecResult:
        try:
            r = await sb.commands.run(cmd, user="root", timeout=timeout)
            return ExecResult(r.exit_code, (r.stdout or "") + (r.stderr or ""))
        except Exception as e:  # CommandExitException carries the result
            code = getattr(e, "exit_code", 1)
            return ExecResult(code, (getattr(e, "stdout", "") or "") + (getattr(e, "stderr", "") or str(e)))

    try:
        await seed_task(ex, task)
        print(f"[{task.name}] seeded in {time.monotonic() - t0:.0f}s", flush=True)
        ev = await evaluate(lambda c, t: ex(c, t), task.eval, node_id="x", label="base")
        print(f"[{task.name}] baseline: {ev.summary()}", flush=True)
        insts = load_instances()
        if task.name in insts:
            inst = insts[task.name]
            for label, patch in (("no fix", None), ("gold", inst["patch"])):
                r = await ex(in_dir("/workspace", hidden_check(inst, patch=patch)), 900)
                print(
                    f"[{task.name}] hidden check ({label}): exit {r.exit_code}\n{r.output[-600:]}", flush=True
                )
        else:
            gold = Path(path) / "gold.sh"
            if gold.exists():
                await ex(f"cat > /tmp/gold.sh <<'SFORK_EOF'\n{gold.read_text()}\nSFORK_EOF", 60)
                r = await ex(in_dir(task.eval.workdir, "bash /tmp/gold.sh"), 900)
                print(f"[{task.name}] gold.sh exit {r.exit_code}: {r.output[-800:]}", flush=True)
                ev = await evaluate(lambda c, t: ex(c, t), task.eval, node_id="x", label="gold")
                print(f"[{task.name}] after gold: {ev.summary()}", flush=True)
    finally:
        await sb.kill()


async def main() -> None:
    sem = asyncio.Semaphore(int(os.environ.get("SFORK_VALIDATE_CONCURRENCY", "6")))  # E2B caps live sandboxes

    async def guarded(p: str) -> None:
        async with sem:
            try:
                await run_one(p)
            except Exception as e:
                print(f"[{Path(p).name}] validation error: {type(e).__name__}: {e}", flush=True)

    await asyncio.gather(*(guarded(p) for p in sys.argv[1:]))


if __name__ == "__main__":
    asyncio.run(main())
