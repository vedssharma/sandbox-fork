"""Phase 0 spike: measure sandbox fork / snapshot / pause latency and fork behavior (E2B or Daytona).

Answers the unknowns in PLAN.md section 2:
  1. fork latency vs RAM in use and disk dirtied
  2. hot (memory) snapshot latency and restore latency
  3. latency vs fan-out and tree depth
  4. what a fork duplicates: machine-id, hostname, IP, boot_id, user-space PRNG streams, live processes
  6. whether a parent with live forks can be deleted, and whether a paused VM can be forked

Usage
  E2B_API_KEY=... python bench/phase0_spike.py                     # default sweep on E2B
  DAYTONA_API_KEY=... python bench/phase0_spike.py --backend daytona
  python bench/phase0_spike.py --mem-mib 0 512 --fanout 2 4 --depth 5 --repeats 2
  python bench/phase0_spike.py --fake                               # dry run, no provider calls

Results go to bench/results/phase0-<timestamp>.json plus a Markdown summary on stdout.
Everything the script creates is deleted at the end, leaves first, even on failure.
Billing/storage cost (unknown 5) is not measurable from the API; check the provider dashboard after.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sfork.daytona.base import ExecResult, SandboxBackend  # noqa: E402
from sfork.guest import post_fork_command  # noqa: E402

# A background process that ticks a counter: if it keeps ticking in a fork, process memory was forked.
COUNTER = (
    "nohup sh -c 'i=0; while true; do i=$((i+1)); echo $i > /tmp/sfork-counter; sleep 0.2; done' "
    ">/dev/null 2>&1 &"
)
# A long-lived Python process streaming PRNG output: identical streams across forks = shared PRNG state.
RNG = (
    "nohup python3 -c 'import random,time\nwhile True:\n"
    ' open("/tmp/sfork-rng","a").write(str(random.random())+"\\n"); time.sleep(0.25)\' '
    ">/dev/null 2>&1 &"
)
IDENTITY = (
    "echo machine_id=$(cat /etc/machine-id 2>/dev/null); echo hostname=$(hostname); "
    "echo ip=$(hostname -I 2>/dev/null | awk '{print $1}'); "
    "echo boot_id=$(cat /proc/sys/kernel/random/boot_id); "
    "echo counter=$(cat /tmp/sfork-counter 2>/dev/null); "
    "echo rng_tail=$(tail -n 3 /tmp/sfork-rng 2>/dev/null | tr '\\n' ,)"
)


def hold_memory_cmd(mib: int) -> str:
    # Allocate and touch `mib` MiB in a background process so it is resident RAM.
    return (
        f"nohup python3 -c 'import time; b=bytearray({mib}*1024*1024); "
        "[b.__setitem__(i,1) for i in range(0,len(b),4096)]; time.sleep(10**9)' >/dev/null 2>&1 &"
    )


def dirty_disk_cmd(mib: int) -> str:
    return f"dd if=/dev/urandom of=/tmp/sfork-dirty-{mib} bs=1M count={mib} status=none && sync"


def parse_kv(out: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


@dataclass
class Tracker:
    """Remembers everything we create so cleanup can delete it, children before parents."""

    backend: SandboxBackend
    parent_of: dict[str, str | None] = field(default_factory=dict)
    snapshots: list[str] = field(default_factory=list)
    deleted: set[str] = field(default_factory=set)

    async def create(self, **kw: Any) -> str:
        sid = await self.backend.create(**kw)
        self.parent_of[sid] = None
        return sid

    async def fork(self, sid: str) -> str:
        child = await self.backend.fork(sid)
        self.parent_of[child] = sid
        return child

    async def delete(self, sid: str) -> None:
        await self.backend.delete(sid)
        self.deleted.add(sid)

    async def delete_subtree(self, sid: str) -> None:
        for child in [c for c, p in self.parent_of.items() if p == sid and c not in self.deleted]:
            await self.delete_subtree(child)
        if sid not in self.deleted:
            await self.delete(sid)

    async def cleanup(self) -> list[str]:
        errors = []
        for sid in [s for s, p in self.parent_of.items() if p is None]:
            try:
                await self.delete_subtree(sid)
            except Exception as e:  # keep going; report what leaked
                errors.append(f"sandbox {sid}: {e}")
        for name in self.snapshots:
            try:
                await self.backend.delete_snapshot(name)
            except Exception as e:
                errors.append(f"snapshot {name}: {e}")
        return errors


async def timed(coro) -> tuple[float, Any]:
    t0 = time.perf_counter()
    res = await coro
    return time.perf_counter() - t0, res


async def must(backend: SandboxBackend, sid: str, cmd: str, timeout: int = 600) -> ExecResult:
    res = await backend.exec(sid, cmd, timeout=timeout)
    if not res.ok:
        raise RuntimeError(f"command failed in {sid} ({res.exit_code}): {cmd}\n{res.output[-1000:]}")
    return res


def summarize(xs: list[float]) -> dict[str, float]:
    return {
        "n": len(xs),
        "median_s": round(statistics.median(xs), 3),
        "min_s": round(min(xs), 3),
        "max_s": round(max(xs), 3),
    }


async def spike(args: argparse.Namespace, backend: SandboxBackend) -> dict[str, Any]:
    t = Tracker(backend)
    results: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "args": vars(args),
        "errors": [],
    }
    try:
        dt, root = await timed(t.create(snapshot=args.snapshot))
        results["create_root_s"] = round(dt, 3)
        results["vm_info"] = (await backend.exec(root, "nproc; free -m; df -h /", timeout=30)).output

        # --- 4: what a fork duplicates --------------------------------------------------
        await must(backend, root, COUNTER)
        await must(backend, root, RNG)
        await asyncio.sleep(args.settle)
        child = await t.fork(root)
        await asyncio.sleep(args.settle)
        p_id = parse_kv((await backend.exec(root, IDENTITY, timeout=30)).output)
        c_id = parse_kv((await backend.exec(child, IDENTITY, timeout=30)).output)
        hook = await backend.exec(child, post_fork_command(hostname="sfork-child"), timeout=60)
        c_after = parse_kv((await backend.exec(child, IDENTITY, timeout=30)).output)
        results["fork_identity"] = {
            "parent": p_id,
            "child_before_hook": c_id,
            "child_after_hook": c_after,
            "hook_exit": hook.exit_code,
            "hook_output_tail": hook.output[-300:],
            "same_machine_id_before_hook": p_id.get("machine_id") == c_id.get("machine_id"),
            "same_machine_id_after_hook": p_id.get("machine_id") == c_after.get("machine_id"),
            "same_ip": p_id.get("ip") == c_id.get("ip"),
            "same_boot_id": p_id.get("boot_id") == c_id.get("boot_id"),
            "same_hostname": p_id.get("hostname") == c_id.get("hostname"),
            "counter_process_survived_fork": bool(c_id.get("counter")) and c_id.get("counter") != "0",
            "rng_streams_identical": bool(p_id.get("rng_tail"))
            and p_id.get("rng_tail") == c_id.get("rng_tail"),
        }

        # --- 6: parent deletion while it has forks; forking a paused VM ------------------
        try:
            await backend.delete(root)
            t.deleted.add(root)
            results["delete_parent_with_live_fork"] = "allowed"
        except Exception as e:
            results["delete_parent_with_live_fork"] = f"refused: {type(e).__name__}: {e}"[:400]
        await t.delete(child)
        if root in t.deleted:  # the documented constraint didn't hold; start a new root
            root = await t.create(snapshot=args.snapshot)

        dt_pause, _ = await timed(backend.pause(root))
        try:
            dt_fp, paused_child = await timed(t.fork(root))
            results["fork_of_paused_vm"] = {"supported": True, "fork_s": round(dt_fp, 3)}
            results["fork_of_paused_vm"]["child_state"] = await backend.state(paused_child)
            await t.delete(paused_child)
        except Exception as e:
            results["fork_of_paused_vm"] = {"supported": False, "error": str(e)[:400]}
        dt_resume, _ = await timed(backend.resume(root))
        results["pause_resume_idle"] = {"pause_s": round(dt_pause, 3), "resume_s": round(dt_resume, 3)}

        # --- 1 + 2: latency vs memory / disk size ----------------------------------------
        sweep = []
        mem_total = disk_total = 0
        for mem, disk in zip(args.mem_mib, args.disk_mib, strict=True):
            if mem > mem_total:
                await must(backend, root, hold_memory_cmd(mem - mem_total))
                mem_total = mem
            if disk > disk_total:
                await must(backend, root, dirty_disk_cmd(disk - disk_total))
                disk_total = disk
            await asyncio.sleep(args.settle)
            row: dict[str, Any] = {"mem_mib": mem_total, "disk_mib": disk_total}
            forks, deletes = [], []
            for _ in range(args.repeats):
                dt, c = await timed(t.fork(root))
                forks.append(dt)
                dt, _ = await timed(t.delete(c))
                deletes.append(dt)
            row["fork"] = summarize(forks)
            row["delete"] = summarize(deletes)
            pauses, resumes = [], []
            for _ in range(args.repeats):
                dt, _ = await timed(backend.pause(root))
                pauses.append(dt)
                dt, _ = await timed(backend.resume(root))
                resumes.append(dt)
            row["pause"] = summarize(pauses)
            row["resume"] = summarize(resumes)
            if not args.skip_snapshots:
                name = f"sfork-spike-{int(time.time())}-{mem_total}"
                try:
                    dt, snap = await timed(backend.hot_snapshot(root, name))
                    t.snapshots.append(snap)
                    row["hot_snapshot_s"] = round(dt, 3)
                    dt, restored = await timed(t.create(snapshot=snap))
                    row["restore_from_hot_snapshot_s"] = round(dt, 3)
                    r = await backend.exec(restored, "cat /tmp/sfork-counter", timeout=30)
                    row["restored_processes_running"] = r.ok and r.output.strip() not in ("", "0")
                    await t.delete(restored)
                except Exception as e:
                    row["hot_snapshot_error"] = str(e)[:400]
            sweep.append(row)
            print(f"  size mem={mem_total}MiB disk={disk_total}MiB: {json.dumps(row)}", file=sys.stderr)
        results["size_sweep"] = sweep

        # --- 3a: fan-out --------------------------------------------------------------
        fan = []
        for n in args.fanout:
            t0 = time.perf_counter()
            per = await asyncio.gather(*(timed(t.fork(root)) for _ in range(n)), return_exceptions=True)
            wall = time.perf_counter() - t0
            ok = [p for p in per if not isinstance(p, BaseException)]
            errs = [str(p)[:200] for p in per if isinstance(p, BaseException)]
            fan.append(
                {
                    "n": n,
                    "wall_s": round(wall, 3),
                    "per_fork": summarize([d for d, _ in ok]) if ok else None,
                    "errors": errs,
                }
            )
            await asyncio.gather(*(t.delete(c) for _, c in ok))
        results["fanout"] = fan

        # --- 3b: depth (fork of fork of fork ...) ---------------------------------------
        chain, depth_rows, cur = [], [], root
        for d in range(1, args.depth + 1):
            dt, cur = await timed(t.fork(cur))
            chain.append(cur)
            depth_rows.append({"depth": d, "fork_s": round(dt, 3)})
        probe = await backend.exec(cur, "cat /tmp/sfork-counter", timeout=30) if chain else None
        results["depth"] = {
            "levels": depth_rows,
            "deepest_counter_alive": bool(probe and probe.ok and probe.output.strip()),
        }
        for sid in reversed(chain):
            await t.delete(sid)
    except Exception as e:
        results["errors"].append(f"{type(e).__name__}: {e}")
    finally:
        results["cleanup_errors"] = await t.cleanup()
        results["finished_at"] = datetime.now(UTC).isoformat()
    return results


def markdown(results: dict[str, Any]) -> str:
    title = "## Phase 0 spike results" + (" (DRY RUN, fake backend)" if results["args"]["fake"] else "")
    out = [title, ""]
    out.append(f"- create root VM: {results.get('create_root_s')} s")
    fi = results.get("fork_identity", {})
    for k in (
        "counter_process_survived_fork",
        "rng_streams_identical",
        "same_machine_id_before_hook",
        "same_machine_id_after_hook",
        "same_ip",
        "same_hostname",
        "same_boot_id",
    ):
        if k in fi:
            out.append(f"- {k}: {fi[k]}")
    out.append(f"- delete parent with live fork: {results.get('delete_parent_with_live_fork')}")
    out.append(f"- fork of paused VM: {results.get('fork_of_paused_vm')}")
    out.append(f"- pause/resume (idle): {results.get('pause_resume_idle')}")
    out += [
        "",
        "| RAM MiB | disk MiB | fork med s | pause med s | resume med s | hot snap s | restore s |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results.get("size_sweep", []):
        out.append(
            f"| {r['mem_mib']} | {r['disk_mib']} | {r['fork']['median_s']} | {r['pause']['median_s']} "
            f"| {r['resume']['median_s']} | {r.get('hot_snapshot_s', r.get('hot_snapshot_error', '-'))} "
            f"| {r.get('restore_from_hot_snapshot_s', '-')} |"
        )
    out += ["", "| fan-out | wall s | per-fork median s | errors |", "|---|---|---|---|"]
    for f in results.get("fanout", []):
        med = f["per_fork"]["median_s"] if f["per_fork"] else "-"
        out.append(f"| {f['n']} | {f['wall_s']} | {med} | {len(f['errors'])} |")
    depth = results.get("depth", {})
    if depth.get("levels"):
        lv = ", ".join(f"d{x['depth']}={x['fork_s']}s" for x in depth["levels"])
        out += ["", f"- fork latency by depth: {lv}"]
        out.append(f"- process alive at deepest level: {depth['deepest_counter_alive']}")
    if results.get("errors"):
        out += ["", "**Errors:** " + "; ".join(results["errors"])]
    if results.get("cleanup_errors"):
        out += ["", "**Cleanup left resources behind:** " + "; ".join(results["cleanup_errors"])]
    return "\n".join(out)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", choices=["e2b", "daytona"], help="default: $SFORK_BACKEND, else e2b")
    p.add_argument("--snapshot", help="root template/snapshot (e2b: base, daytona: daytona-vm-small)")
    p.add_argument("--mem-mib", type=int, nargs="+", default=[0, 512, 2048])
    p.add_argument("--disk-mib", type=int, nargs="+", default=[0, 512, 2048])
    p.add_argument("--fanout", type=int, nargs="+", default=[2, 4, 8])
    p.add_argument("--depth", type=int, default=10)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--settle", type=float, default=2.0, help="seconds to let background processes run")
    p.add_argument("--skip-snapshots", action="store_true")
    p.add_argument("--fake", action="store_true", help="dry run against the in-process fake backend")
    p.add_argument("--out", type=Path, default=Path(__file__).parent / "results")
    a = p.parse_args(argv)
    if len(a.mem_mib) != len(a.disk_mib):
        p.error("--mem-mib and --disk-mib need the same number of values")
    return a


def make_fake() -> SandboxBackend:
    from sfork.daytona.fake import FakeBackend

    # Let the dry run's real shell commands "succeed" so the script's control flow is exercised.
    def permissive(sb, cmd):
        if "sfork-guest post-fork" in cmd or cmd.startswith("cat "):
            return None
        return ExecResult(0, "")

    return FakeBackend(handlers=[permissive])


async def amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.fake:
        backend = make_fake()
    else:
        from sfork.daytona import make_backend

        backend = make_backend(args.backend)
    try:
        results = await spike(args, backend)
    finally:
        await backend.close()
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"phase0-{'fake-' if args.fake else ''}{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(results, indent=2, default=str))
    print(markdown(results))
    print(f"\nraw results: {path}", file=sys.stderr)
    return 1 if results["errors"] or results["cleanup_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
