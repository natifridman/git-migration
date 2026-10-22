from dataclasses import dataclass, field
from typing import Literal


@dataclass
class TransferProgress:
    status: Literal["pending", "finished", "failed"]
    handle: dict
    destination_project_id: int | None = None
    failures: list[str] = field(default_factory=list)


class AmbiguousOperation(RuntimeError):
    """A mutation might have succeeded; operator reconciliation is required."""


class TargetOccupied(RuntimeError):
    """The destination project exists and must not be overwritten or adopted."""
