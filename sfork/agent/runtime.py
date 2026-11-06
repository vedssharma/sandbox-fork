"""The tool-use loop for one branch (PLAN.md section 5.1).

A `Branch` is the agent state pinned to one tree node: its message history, model and counters.
`Agent.run` drives a branch until it calls `done`, asks to `fork` or `rewind`, runs out of steps, or
fails, and returns a `Stop` saying which. Forking and rewinding belong to the orchestrator
(`sfork.orchestrator.explore`): it forks the sandbox and calls `Branch.fork` once per child to copy
the conversation, or forks a fresh node from a checkpoint and calls `Branch.rewind_to`.

Checkpoints happen inside a turn, through the `checkpointer` callback: the orchestrator freezes the
branch's node as a restore point and moves the branch onto a fork of it, and the rest of the turn
runs there. A `CheckpointRecord` remembers where in the conversation that happened, so a rewind can
cut the history back to exactly that point and answer the turn's tool calls as if the abandoned
timeline never ran. The history stays append-only from the checkpoint's prefix, so it remains valid
under preserved thinking and reuses the prompt cache.
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
from .policy import CheckpointPolicy
from .tools import ALL_TOOLS, TOOLS_WITHOUT_REWIND

ExecFn = Callable[[str, str, int], Awaitable[ExecResult]]  # (node_id, command, timeout)
EventFn = Callable[[dict[str, Any]], None]
# (branch, label, auto) -> checkpoint id; moves the branch onto a fresh working node. Raises on refusal.
CheckpointFn = Callable[["Branch", str, bool], Awaitable[str]]
REWIND_TAG = "[sfork-rewound:"
OUTPUT_LIMIT = 12_000
WRITE_CHUNK = 48_000


@dataclass
class CheckpointRecord:
    """Where in a branch's conversation a checkpoint was taken (see `Branch.rewind_to`)."""

    id: str  # the checkpoint node id
    label: str
    auto: bool
    msg_len: int  # len(messages) right after the assistant turn that took it
    results_before: list[dict[str, Any]]  # tool results of that turn produced before the checkpoint
    after_ids: list[str]  # tool_use ids in that turn not yet run at checkpoint time
    tool_use_id: str | None = None  # the agent's checkpoint call, if it made one
    result_text: str = ""


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
    checkpoints: list[CheckpointRecord] = field(default_factory=list)  # oldest first, this timeline only
    lessons: list[str] = field(default_factory=list)  # from every rewind so far, oldest first
    rewinds: int = 0

    def find_checkpoint(self, checkpoint_id: str) -> CheckpointRecord | None:
        return next((c for c in self.checkpoints if c.id == checkpoint_id), None)

    def rewind_to(self, rec: CheckpointRecord, *, node_id: str, lesson: str) -> None:
        """Cut the conversation back to `rec` and continue on `node_id`, a fresh fork of it.

        The kept history is the prefix up to the assistant turn that took the checkpoint. The next
        user turn answers that turn's tool calls: the ones that ran before the checkpoint keep their
        results, the checkpoint call (if the agent made it) gets its result, and calls after it,
        including the command an auto-checkpoint guarded, are reported as not run. Checkpoints made
        after `rec` belonged to the abandoned timeline and are forgotten.
        """
        self.rewinds += 1
        if lesson:
            self.lessons.append(lesson)
        self.checkpoints = self.checkpoints[: self.checkpoints.index(rec) + 1]
        self.node_id = node_id
        turn: list[Any] = list(rec.results_before)
        if rec.tool_use_id is not None:
            turn.append(_result(rec.tool_use_id, rec.result_text))
        for tid in rec.after_ids:
            why = (
                "the auto-checkpoint was taken just before this command"
                if rec.auto and tid == rec.after_ids[0]
                else "it came after the checkpoint in the same turn"
            )
            turn.append(_result(tid, f"Not run: you rewound to checkpoint {rec.id}, and {why}.", error=True))
        lessons = "\n".join(f"{i}. {ln}" for i, ln in enumerate(self.lessons, 1)) or "(none given)"
        note = (
            f"{REWIND_TAG}{rec.id}] You rewound to checkpoint {rec.id} ({rec.label!r}). Everything after "
            "it has been undone: files, databases, processes and this conversation are exactly as they "
            "were at that point, so do not try to repair the old state; it no longer exists. "
            f"This is rewind #{self.rewinds} in this branch. Lessons from the attempts you abandoned:\n"
            f"{lessons}\nUse them: do not repeat what failed."
        )
        turn.append({"type": "text", "text": note})
        self.messages = [*self.messages[: rec.msg_len], {"role": "user", "content": turn}]

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
    kind: Literal["done", "fork", "rewind", "budget", "failed"]
    summary: str = ""
    alternatives: list[dict[str, str]] = field(default_factory=list)
    tool_use_id: str | None = None
    pending_results: list[dict[str, Any]] = field(default_factory=list)
    checkpoint_id: str | None = None  # rewind target
    lesson: str = ""


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
        checkpointer: CheckpointFn | None = None,
        policy: CheckpointPolicy | None = None,
    ) -> None:
        self.llm = llm
        self.exec = exec_fn
        self.eval_cfg = eval_cfg
        self.system = system
        self.max_tokens = max_tokens
        self.tool_timeout = tool_timeout
        self.on_event = on_event or (lambda e: None)
        self.checkpointer = checkpointer
        self.policy = policy if checkpointer is not None else None
        self.tools = ALL_TOOLS if checkpointer is not None else TOOLS_WITHOUT_REWIND

    async def run(self, br: Branch, *, max_steps: int) -> Stop:
        nudged = False
        while br.steps < max_steps:
            resp = await self.llm.complete(
                model=br.model,
                system=self.system,
                messages=br.messages,
                tools=self.tools,
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

            rewind_call = next((u for u in uses if block_get(u, "name") == "rewind"), None)
            if rewind_call is not None and self.checkpointer is not None:
                # Nothing else in this turn runs: whatever it did would be undone anyway.
                args = block_get(rewind_call, "input") or {}
                stop = Stop(
                    "rewind",
                    tool_use_id=block_get(rewind_call, "id"),
                    checkpoint_id=str(args.get("checkpoint_id", "")).strip(),
                    lesson=str(args.get("lesson", "")).strip(),
                    pending_results=[
                        _result(
                            block_get(u, "id"), "Not run: rewind was called in the same turn.", error=True
                        )
                        for u in uses
                        if u is not rewind_call
                    ],
                )
                self._emit(br, "rewind_request", checkpoint=stop.checkpoint_id, lesson=stop.lesson[:300])
                return stop

            results: list[dict[str, Any]] = []
            fork_call = done_call = None
            for i, tu in enumerate(uses):
                name, tid = block_get(tu, "name"), block_get(tu, "id")
                if name == "checkpoint" and self.checkpointer is not None:
                    label = str((block_get(tu, "input") or {}).get("label", "")).strip() or "checkpoint"
                    results.append(await self._checkpoint(br, uses, i, results, label=label, call_id=tid))
                elif name == "bash" and (risky := self._risky(tu)) and not self._just_checkpointed(br, tid):
                    try:
                        cp = await self._checkpoint(br, uses, i, results, label=f"before: {risky[:100]}")
                        note = (
                            f"[auto-checkpoint {cp} saved just before this command; "
                            f"rewind(checkpoint_id='{cp}') returns to the state before it]\n"
                        )
                    except Exception as e:
                        note = f"[auto-checkpoint failed ({e}); this command ran without one]\n"
                    res = await self._run_tool(br, tu)
                    res["content"] = note + res["content"]
                    results.append(res)
                elif name == "fork" and fork_call is None:
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

    @staticmethod
    def _just_checkpointed(br: Branch, tool_use_id: str) -> bool:
        """The agent checkpointed earlier in this turn and nothing has run since: no second one."""
        last = br.checkpoints[-1] if br.checkpoints else None
        return (
            last is not None
            and last.msg_len == len(br.messages)
            and bool(last.after_ids)
            and last.after_ids[0] == tool_use_id
        )

    def _risky(self, tu: Any) -> str | None:
        """The risky piece of a bash call's command, if the policy says to checkpoint first."""
        if self.policy is None:
            return None
        return self.policy.match(str((block_get(tu, "input") or {}).get("command", "")))

    async def _checkpoint(
        self,
        br: Branch,
        uses: list[Any],
        i: int,
        results: list[dict[str, Any]],
        *,
        label: str,
        call_id: str | None = None,
    ) -> Any:
        """Checkpoint mid-turn. For an agent call returns its tool result; for an auto-checkpoint
        returns the checkpoint id (and raises if it could not be taken)."""
        assert self.checkpointer is not None
        auto = call_id is None
        try:
            cp = await self.checkpointer(br, label, auto)
        except Exception as e:
            if auto:
                raise
            return _result(call_id, f"Checkpoint failed: {e}", error=True)
        text = (
            f"Checkpoint {cp} saved ({label!r}). rewind(checkpoint_id='{cp}', lesson=...) returns the "
            "machine and this conversation to this exact point."
        )
        later = uses[i:] if auto else uses[i + 1 :]
        br.checkpoints.append(
            CheckpointRecord(
                id=cp,
                label=label,
                auto=auto,
                msg_len=len(br.messages),
                results_before=list(results),
                after_ids=[block_get(u, "id") for u in later],
                tool_use_id=call_id,
                result_text=text,
            )
        )
        self._emit(br, "checkpoint", checkpoint=cp, label=label, auto=auto)
        return cp if auto else _result(call_id, text)

    def resume_after_failed_rewind(self, br: Branch, stop: Stop, reason: str) -> None:
        """Answer a rewind request the orchestrator refused, so the branch can keep going."""
        valid = ", ".join(f"{c.id} ({c.label!r})" for c in br.checkpoints) or "none"
        br.messages.append(
            {
                "role": "user",
                "content": [
                    *stop.pending_results,
                    _result(
                        stop.tool_use_id or "",
                        f"Rewind refused: {reason} Checkpoints you can rewind to: {valid}.",
                        error=True,
                    ),
                ],
            }
        )

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
