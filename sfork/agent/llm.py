"""The model client the agent loop talks to.

`AnthropicLLM` calls the Messages API. `ScriptedLLM` replays canned turns so the loop, forking and
evaluation can be tested without network access or spend.

Conversation history is append-only: each assistant turn goes back exactly as the API returned it
(thinking blocks included), and forking a conversation copies the list and appends to each copy.
That keeps every branch valid under preserved thinking and lets branches share the cached prefix.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Protocol

DEFAULT_MODEL = os.environ.get("SFORK_MODEL", "claude-opus-5-5")
DEFAULT_EFFORT = os.environ.get("SFORK_EFFORT", "medium")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    calls: int = 0

    def add(self, other: Any) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            setattr(self, name, getattr(self, name) + (getattr(other, name, 0) or 0))
        self.calls += 1

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass
class LLMResponse:
    content: list[Any]  # content blocks, returned to the API unchanged on the next turn
    stop_reason: str | None
    usage: Any = None
    model: str | None = None


class LLM(Protocol):
    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Any],
        tools: Sequence[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse: ...


def block_get(block: Any, key: str, default: Any = None) -> Any:
    """Read a field from an SDK content block or a plain dict."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


class AnthropicLLM:
    """Messages API client: adaptive thinking (the model default), explicit effort, prompt caching,
    and server-side refusal fallbacks."""

    def __init__(
        self,
        *,
        effort: str = DEFAULT_EFFORT,
        fallbacks: bool = True,
        client: Any = None,
        max_retries: int = 4,
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(max_retries=max_retries)
        self._client = client
        self.effort = effort
        self.fallbacks = fallbacks

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Any],
        tools: Sequence[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=list(tools),
            messages=list(messages),
            output_config={"effort": self.effort},
            # Caches through the last block, so sibling branches reuse the shared fork-point prefix.
            cache_control={"type": "ephemeral"},
        )
        if self.fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        msg = await self._client.beta.messages.create(**kwargs)
        return LLMResponse(
            content=list(msg.content), stop_reason=msg.stop_reason, usage=msg.usage, model=msg.model
        )

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


# --- test double -------------------------------------------------------------------------------


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def tool_use(name: str, input: dict[str, Any], id: str | None = None) -> SimpleNamespace:
    import uuid

    return SimpleNamespace(type="tool_use", id=id or f"toolu_{uuid.uuid4().hex[:10]}", name=name, input=input)


@dataclass
class ScriptedLLM:
    """Replays turns chosen by `script(branch, turn_index, messages) -> list[block]`.

    `branch` is the branch name the conversation was forked into (None for the root), read from the
    fork tool result, so one script can drive every branch of a run.
    """

    script: Callable[[str | None, int, list[Any]], list[Any]]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete(self, *, model, system, messages, tools, max_tokens) -> LLMResponse:
        branch = _branch_of(messages)
        turn = sum(1 for m in messages if block_get(m, "role") == "assistant")
        # turns since the fork, so a branch script starts at 0
        if branch is not None:
            turn -= _fork_turn(messages)
        self.calls.append({"model": model, "branch": branch, "turn": turn, "n_messages": len(messages)})
        blocks = self.script(branch, turn, list(messages))
        stop = "tool_use" if any(block_get(b, "type") == "tool_use" for b in blocks) else "end_turn"
        usage = SimpleNamespace(input_tokens=100, output_tokens=20)
        return LLMResponse(content=blocks, stop_reason=stop, usage=usage, model=model)


BRANCH_TAG = "[sfork-branch:"


def _branch_of(messages: Sequence[Any]) -> str | None:
    for m in reversed(messages):
        content = block_get(m, "content")
        if not isinstance(content, list):
            content = [{"type": "text", "text": content}]
        for b in content:
            t = block_get(b, "content") if block_get(b, "type") == "tool_result" else block_get(b, "text")
            if isinstance(t, str) and BRANCH_TAG in t:
                return t.split(BRANCH_TAG, 1)[1].split("]", 1)[0]
    return None


def _fork_turn(messages: Sequence[Any]) -> int:
    """Number of assistant turns up to and including the most recent fork."""
    count = last = 0
    for m in messages:
        role = block_get(m, "role")
        if role == "assistant":
            count += 1
        content = block_get(m, "content")
        if role == "user" and isinstance(content, list | str):
            items = content if isinstance(content, list) else [{"type": "text", "text": content}]
            for b in items:
                t = block_get(b, "content") if block_get(b, "type") == "tool_result" else block_get(b, "text")
                if isinstance(t, str) and BRANCH_TAG in t:
                    last = count
    return last
