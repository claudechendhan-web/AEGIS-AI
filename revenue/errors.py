"""Errors raised by the revenue evaluation subsystem.

Local to the package so the subsystem can be removed without editing
`core/errors.py` or `pyproject.toml`.
"""

from core.errors import AegisError


class RevenueError(AegisError):
    """Base exception for revenue evaluation failures."""


class UnsubstantiatedOpportunityError(RevenueError):
    """Raised when an opportunity is asserted without retrievable evidence.

    This is the most important refusal in the subsystem. An opportunity with
    no evidence is a guess wearing a price tag, and acting on guesses is how a
    budget disappears.
    """


class NoAffordabilityError(RevenueError):
    """Raised when an opportunity costs more than the agent can spend.

    Note this is about the *spendable* envelope, not the reserve.
    """


class UnknownOpportunityError(RevenueError):
    """Raised when an outcome refers to an opportunity that was never recorded."""


class InvalidOpportunityError(RevenueError, ValueError):
    """Raised when an opportunity is malformed."""
