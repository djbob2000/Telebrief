"""Process-local registry for the application's shared infrastructure.

The registry hands long-lived resources (connection pool, unit of work,
Procrastinate app) to task and application-service code without ad-hoc
globals or reconnection. It owns no connections itself; repositories still
receive explicit transaction connections from the caller.
"""

from __future__ import annotations

from src.bootstrap import ApplicationInfrastructure

__all__ = ["clear_runtime", "get_runtime", "install_runtime"]

_runtime: ApplicationInfrastructure | None = None


def install_runtime(infrastructure: ApplicationInfrastructure) -> None:
    """Install the process-wide infrastructure exactly once."""
    global _runtime
    if _runtime is not None:
        raise RuntimeError("Telebrief runtime already installed")
    _runtime = infrastructure


def get_runtime() -> ApplicationInfrastructure:
    """Return the installed infrastructure, failing clearly when absent."""
    if _runtime is None:
        raise RuntimeError("Telebrief runtime is not initialized")
    return _runtime


def clear_runtime(infrastructure: ApplicationInfrastructure) -> None:
    """Clear the registry when the installed instance is ``infrastructure``.

    Mismatched instances never evict the live runtime: graceful shutdown of a
    stale duplicate must not tear down the process reference that other
    components still hold.
    """
    global _runtime
    if _runtime is infrastructure:
        _runtime = None
