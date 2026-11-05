"""The tool-use loop for one branch (PLAN.md section 5.1).

A `Branch` is the agent state pinned to one tree node: its message history, model and counters.
`Agent.run` drives a branch until it calls `done`, asks to `fork`, runs out of steps, or fails, and
returns a `Stop` saying which. Forking itself belongs to the orchestrator (`sfork.orchestrator.explore`),
which forks the sandbox and then calls `Branch.fork` once per child to copy the conversation.
"""

from __future__ import annotations

import base64
import posixpath
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..daytona.base import ExecResult
from ..eval.config import EvalConfig
from ..eval.evaluator import evaluate, in_dir
from .llm import BRANCH_TAG, LLM, Usage, block_get
from .tools import ALL_TOOLS

ExecFn = Callable[[str, str, int], Awaitable[ExecResult]]  # (node_id, command, timeout)
EventFn = Callable[[dict[str, Any]], None]
OUTPUT_LIMIT = 12_000
WRITE_CHUNK = 48_000


@dataclass
class Branch:
    node_id: str
    model: str
    messages: list[Any]
    name: str | None = None
    plan: str | None = None
    steps: int = 0
    usage: Usage = field(default_factory=Usage)
    tool_calls: int = 0

    def fork(
        self,
        *,
        node_id: str,
        model: str,
        name: str,
        plan: str,
        siblings: list[str],
        tool_use_id: str | None,
        pending_results: list[dict[str, Any]],
    ) -> Branch:
        """Copy this conversation into a child pinned to `node_id`, told to pursue one alternative.

        Append-only: the child's history is this history plus one new user turn, which carries the
        tool results of the forking turn (the fork result names the child's alternative).
        """
        others = ", ".join(s for s in siblings if s != name) or "none"
        note = (
            f"{BRANCH_TAG}{name}] Forked. You are branch '{name}' and now have your own copy of the "
            f"machine. Your approach: {plan}\nOther branches are trying: {others}. Commit fully to "
            "your approach; don't hedge or implement the others. Use run_tests to check your work, "
            "then call done. Further forking is only allowed if the run's budget permits it."
        )
        if tool_use_id is not None:
            turn: list[Any] = [*pending_results, _result(tool_use_id, note)]
        else:  # orchestrator-initiated fork: no tool call to answer
            turn = [{"type": "text", "text": note}]
        return Branch(
            node_id=node_id,
            model=model,
            messages=[*self.messages, {"role": "user", "content": turn}],
            name=name,
            plan=plan,
        )


@dataclass
class Stop:
    kind: Literal["done", "fork", "budget", "failed"]
    summary: str = ""
    alternatives: list[dict[str, str]] = field(default_factory=list)
    tool_use_id: str | None = None
    pending_results: list[dict[str, Any]] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        llm: LLM,
        exec_fn: ExecFn,
        eval_cfg: EvalConfig,
        *,
        system: str,
        max_tokens: int = 16_000,
        tool_timeout: int = 300,
        on_event: EventFn | None = None,
    ) -> None:
        self.llm = llm
        self.exec = exec_fn
        self.eval_cfg = eval_cfg
        self.system = system
        self.max_tokens = max_tokens
        self.tool_timeout = tool_timeout
        self.on_event = on_event or (lambda e: None)

    async def run(self, br: Branch, *, max_steps: int) -> Stop:
        nudged = False
        while br.steps < max_steps:
            resp = await self.llm.complete(
                model=br.model,
                system=self.system,
                messages=br.messages,
                tools=ALL_TOOLS,
                max_tokens=self.max_tokens,
            )
            br.steps += 1
            br.usage.add(resp.usage)
            br.messages.append({"role": "assistant", "content": resp.content})
            said = " ".join(block_get(b, "text", "") for b in resp.content if block_get(b, "type") == "text")
            if said.strip():
                self._emit(br, "say", text=said.strip()[:500])
            if resp.stop_reason == "refusal":
                return Stop("failed", summary="model declined the request")
            uses = [b for b in resp.content if block_get(b, "type") == "tool_use"]
            if not uses:
                if nudged:
                    return Stop("done", summary=said.strip() or "(no summary)")
                nudged = True
                br.messages.append(
                    {"role": "user", "content": "Keep working with the tools, and call done when finished."}
                )
                continue

            results: list[dict[str, Any]] = []
            fork_call = done_call = None
            for tu in uses:
                name, tid = block_get(tu, "name"), block_get(tu, "id")
                if name == "fork" and fork_call is None:
                    fork_call = tu
                elif name == "fork":
                    results.append(_result(tid, "Only one fork per turn; this one was ignored.", error=True))
                elif name == "done" and done_call is None:
                    done_call = tu
                elif name == "done":
                    results.append(_result(tid, "done was already called this turn.", error=True))
                else:
                    results.append(await self._run_tool(br, tu))
            if fork_call is not None:
                if done_call is not None:
                    results.append(
                        _result(
                            block_get(done_call, "id"), "Ignored: you forked in the same turn.", error=True
                        )
                    )
                alts = list((block_get(fork_call, "input") or {}).get("alternatives") or [])
                self._emit(br, "fork_request", alternatives=[a.get("name") for a in alts])
                return Stop(
                    "fork", alternatives=alts, tool_use_id=block_get(fork_call, "id"), pending_results=results
                )
            if done_call is not None:
                summary = str((block_get(done_call, "input") or {}).get("summary", ""))
                results.append(_result(block_get(done_call, "id"), "Submitted for evaluation."))
                br.messages.append({"role": "user", "content": results})
                self._emit(br, "done", summary=summary[:500])
                return Stop("done", summary=summary)
            br.messages.append({"role": "user", "content": results})
        self._emit(br, "budget", steps=br.steps)
        return Stop("budget", summary=f"stopped after {br.steps} steps")

    def resume_after_failed_fork(self, br: Branch, stop: Stop, reason: str) -> None:
        """Answer a fork request that the orchestrator refused, so the branch can keep going."""
        br.messages.append(
            {
                "role": "user",
                "content": [
                    *stop.pending_results,
                    _result(
                        stop.tool_use_id or "",
                        f"Fork refused: {reason} Continue in this branch with the approach you judge best.",
                        error=True,
                    ),
                ],
            }
        )

    # tools ----------------------------------------------------------------------------------------
    async def _run_tool(self, br: Branch, tu: Any) -> dict[str, Any]:
        name, tid = block_get(tu, "name"), block_get(tu, "id")
        args = block_get(tu, "input") or {}
        br.tool_calls += 1
        wd = self.eval_cfg.workdir
        try:
            if name == "bash":
                cmd = str(args.get("command", ""))
                self._emit(br, "tool", name=name, arg=cmd[:200])
                res = await self.exec(br.node_id, in_dir(wd, cmd), self.tool_timeout)
                return _result(tid, f"exit code {res.exit_code}\n{_clip(res.output)}", error=not res.ok)
            if name == "read_file":
                path = _abs(wd, str(args.get("path", "")))
                self._emit(br, "tool", name=name, arg=path)
                res = await self.exec(br.node_id, f"cat -- {shlex.quote(path)}", 60)
                return _result(tid, _clip(res.output) if res.ok else res.output[-2000:], error=not res.ok)
            if name == "write_file":
                path = _abs(wd, str(args.get("path", "")))
                content = str(args.get("content", ""))
                self._emit(br, "tool", name=name, arg=f"{path} ({len(content)} chars)")
                res = await write_file(lambda c, t: self.exec(br.node_id, c, t), path, content)
                return _result(tid, f"wrote {path}" if res.ok else res.output[-2000:], error=not res.ok)
            if name == "run_tests":
                self._emit(br, "tool", name=name, arg="")
                ev = await evaluate(
                    lambda c, t: self.exec(br.node_id, c, t), self.eval_cfg, node_id=br.node_id
                )
                tail = ev.raw.get("tests", "")[-3000:]
                return _result(tid, f"{ev.summary()}\n--- test output (tail) ---\n{tail}")
            return _result(tid, f"unknown tool {name!r}", error=True)
        except Exception as e:  # report backend failures to the model instead of crashing the branch
            return _result(tid, f"{type(e).__name__}: {e}", error=True)

    def _emit(self, br: Branch, kind: str, **data: Any) -> None:
        self.on_event({"type": kind, "node": br.node_id, "branch": br.name, "step": br.steps, **data})


async def write_file(
    exec_: Callable[[str, int], Awaitable[ExecResult]], path: str, content: str
) -> ExecResult:
    """Write a file through exec only (works on every backend), in base64 chunks."""
    data = base64.b64encode(content.encode()).decode()
    q = shlex.quote(path)
    res = await exec_(f"mkdir -p {shlex.quote(posixpath.dirname(path) or '.')} && : > {q}", 60)
    for i in range(0, len(data), WRITE_CHUNK):
        if not res.ok:
            break
        res = await exec_(f"printf %s {data[i : i + WRITE_CHUNK]} | base64 -d >> {q}", 60)
    return res


def _abs(workdir: str, path: str) -> str:
    return path if path.startswith("/") else posixpath.normpath(posixpath.join(workdir, path))


def _result(tool_use_id: str, content: str, *, error: bool = False) -> dict[str, Any]:
    r: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content or "(no output)",
    }
    if error:
        r["is_error"] = True
    return r


def _clip(s: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(s) <= limit:
        return s
    return s[: limit // 3] + f"\n...[{len(s) - limit} chars truncated]...\n" + s[-2 * limit // 3 :]
