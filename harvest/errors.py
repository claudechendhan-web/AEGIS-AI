"""Errors raised by the harvest worker."""

from core.errors import AegisError


class HarvestError(AegisError):
    """Base exception for harvest failures."""


class DeadlineReachedError(HarvestError):
    """Raised when the wall-clock budget is exhausted.

    The worker treats this as a normal, successful stop. Running out of time
    is the expected outcome of a bounded job, not a failure.
    """


class FrontierExhaustedError(HarvestError):
    """Raised when the frontier is empty and no new seeds were produced."""


class SourceConfigError(HarvestError):
    """Raised when the seed source list is malformed."""
