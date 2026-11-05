from .base import BackendError, ExecResult, ParentHasChildrenError, SandboxBackend
from .fake import FakeBackend

__all__ = [
    "BackendError",
    "DaytonaBackend",
    "ExecResult",
    "FakeBackend",
    "ParentHasChildrenError",
    "SandboxBackend",
]


def __getattr__(name: str):
    # Import lazily so tests and the fake don't need the Daytona SDK installed.
    if name == "DaytonaBackend":
        from .real import DaytonaBackend

        return DaytonaBackend
    raise AttributeError(name)
