"""Policy-initiated checkpoints (PLAN.md section 4.2, item 3).

Before a bash command that looks hard to undo, the agent loop transparently checkpoints the machine,
so the agent can rewind to just before it. Matching is a heuristic on the command text: it should
catch the commands that change durable state (migrations, destructive SQL, recursive deletes, history
rewrites) and skip the ones that only read (`cat migrate.sh`, `grep DROP`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

DEFAULT_PATTERNS: tuple[str, ...] = (
    # migration runners: ./migrate.sh, python manage.py migrate, alembic upgrade head, rails db:migrate
    r"(^|[\s;&|/(])migrate(\.sh|\.py)?(\s|$|[;&|)])",
    r"\balembic\s+(upgrade|downgrade|stamp)\b",
    r"\b(rails|rake|bin/rails)\s+db:",
    r"\b(flyway|liquibase)\b.*\b(migrate|update)\b",
    r"\b(knex|sequelize|prisma)\b.*\bmigrat",
    # destructive SQL, wherever it appears (psql -c, sqlite3, heredocs)
    r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA|COLUMN|INDEX)\b",
    r"\bALTER\s+TABLE\b",
    r"\bDELETE\s+FROM\b",
    r"\bUPDATE\s+\w+\s+SET\b",
    # files and history
    r"\brm\s+(-\w*[rR]\w*|--recursive)\b",
    r"\bgit\s+(reset\s+--hard|clean\s+-\w*f|push\s+(-f|--force))",
)

# Pieces of a command line that only look at things (or only set up the shell) are never risky.
_HARMLESS = re.compile(
    r"^\s*(cat|less|more|head|tail|grep|egrep|rg|ls|wc|file|stat|diff|sed\s+-n|awk|sort|uniq|"
    r"cd|pwd|echo|printf|true|set|export|sleep|which|command\s+-v|"
    r"git\s+(diff|show|log|status|blame))\b"
)
# A database client given no inline command (-c/-e) is running a script: stdin, -f, a heredoc.
_DB_CLIENT = re.compile(r"^\s*(sudo\s+(-u\s+\S+\s+)?)?(psql|mysql|sqlite3)\b")
_INLINE_SQL = re.compile(r"\s(-[A-Za-z]*c|--command|-e|--execute)(\s|=|$)")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n.*?\n\s*\2[ \t]*(?=\n|$)", re.S)
_CHAIN = re.compile(r"&&|\|\||;|\||\n")


@dataclass
class CheckpointPolicy:
    patterns: tuple[str, ...] = DEFAULT_PATTERNS
    _compiled: list[re.Pattern[str]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._compiled = [re.compile(p, re.IGNORECASE) for p in self.patterns]

    @classmethod
    def from_config(cls, d: dict | None) -> CheckpointPolicy | None:
        """`[checkpoints]` in sandbox-fork.toml: `auto = false` turns it off, `before = [...]` replaces
        the default patterns, `also_before = [...]` adds to them."""
        d = d or {}
        if d.get("auto", True) is False:
            return None
        pats: Iterable[str] = d.get("before", DEFAULT_PATTERNS)
        return cls(tuple(pats) + tuple(d.get("also_before", ())))

    def match(self, command: str) -> str | None:
        """The piece of `command` that makes it risky, or None.

        The command is split into its pieces (`;`, `&&`, `|`, newlines), heredoc bodies being
        written to a file are ignored, and each piece that isn't a harmless reader is checked.
        """
        text = _HEREDOC.sub(lambda m: f"<<{m.group(2)}{m.group(3)}", command)
        for part in _CHAIN.split(text):
            if not part.strip() or _HARMLESS.match(part):
                continue
            if (_DB_CLIENT.match(part) and not _INLINE_SQL.search(part)) or any(
                rx.search(part) for rx in self._compiled
            ):
                return " ".join(part.split())
        return None
