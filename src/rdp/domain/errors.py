"""Domain-level exceptions. Raised at construction time, never swallowed by adapters."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every error the domain layer raises."""


class InvariantViolation(DomainError):
    """A domain invariant (design §8.4) was violated."""


class IllegalStageTransition(DomainError):
    """An `IngestionStage` transition that the state machine does not permit."""


class BudgetTooSmall(DomainError):
    """The export budget cannot fit a single whole episode (design §6: never truncate)."""
