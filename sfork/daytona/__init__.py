from .base import BackendError, ExecResult, ParentHasChildrenError, SandboxBackend
from .fake import FakeBackend

__all__ = [
    "BACKENDS",
    "BackendError",
    "DaytonaBackend",
    "E2BBackend",
    "ExecResult",
    "FakeBackend",
    "LocalBackend",
    "ParentHasChildrenError",
    "SandboxBackend",
    "make_backend",
]

BACKENDS = ("e2b", "daytona", "local")


def make_backend(name: str | None = None, **kwargs) -> SandboxBackend:
    """Build a real backend by name (default: $SFORK_BACKEND, else "e2b")."""
    import os

    name = (name or os.environ.get("SFORK_BACKEND") or "e2b").lower()
    if name == "e2b":
        from .e2b import E2BBackend

        return E2BBackend(**kwargs)
    if name == "daytona":
        from .real import DaytonaBackend

        return DaytonaBackend(**kwargs)
    if name == "local":  # tests / offline dry runs only: runs commands on this host
        from .local import LocalBackend

        return LocalBackend(**kwargs)
    raise ValueError(f"unknown backend {name!r}; expected one of {BACKENDS}")


def __getattr__(name: str):
    # Import lazily so tests and the fake don't need a provider SDK installed.
    if name == "DaytonaBackend":
        from .real import DaytonaBackend

        return DaytonaBackend
    if name == "LocalBackend":
        from .local import LocalBackend

        return LocalBackend
    if name == "E2BBackend":
        from .e2b import E2BBackend

        return E2BBackend
    raise AttributeError(name)
