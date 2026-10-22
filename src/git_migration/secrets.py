"""Resolve secrets only at execution time; exclude raw errors from public output."""

from __future__ import annotations

import os

from git_migration.models import Endpoint


class SafeError(RuntimeError):
    """An operator-facing error constructed only from non-secret local messages."""


def resolve_token(endpoint: Endpoint) -> str:
    value = os.environ.get(endpoint.token_env)
    if not value:
        raise SafeError(f"Required credential environment variable is unset: {endpoint.token_env}")
    return value


def safe_error(error: Exception) -> str:
    """Never serialize arbitrary API responses, Pydantic inputs, or process output."""
    if isinstance(error, SafeError):
        return str(error)
    status = getattr(error, "status_code", None)
    suffix = f" (HTTP {status})" if status else ""
    return f"{type(error).__name__}{suffix}; see the operation and checkpoint for recovery"
