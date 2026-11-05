"""Guest-side hygiene run inside each VM right after it is forked."""

from __future__ import annotations

import shlex
from importlib import resources

MARKER = "sfork-guest post-fork"


def post_fork_script() -> str:
    return resources.files(__package__).joinpath("post_fork.sh").read_text()


def post_fork_command(hostname: str | None = None) -> str:
    """A self-contained shell command that runs the post-fork hook (no install step needed)."""
    env = f"SFORK_HOSTNAME={shlex.quote(hostname)} " if hostname else ""
    return f"{env}bash -c {shlex.quote(post_fork_script())}"
