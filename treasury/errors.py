"""Errors raised by the treasury.

These are defined locally rather than added to `core/errors.py` so the
treasury can be removed without touching a single existing file. They still
inherit from `AegisError`, so callers that already catch treasury failures as
`AegisError` keep working.
"""

from core.errors import AegisError


class TreasuryError(AegisError):
    """Base exception for treasury failures."""


class InvalidAmountError(TreasuryError, ValueError):
    """Raised when an amount is malformed, negative, or too precise."""


class CurrencyMismatchError(TreasuryError, ValueError):
    """Raised when an operation mixes two different currencies."""


class UnknownAccountError(TreasuryError):
    """Raised when an account is referenced that does not exist."""


class UnknownEnvelopeError(TreasuryError):
    """Raised when a budget envelope is referenced that does not exist."""


class InsufficientFundsError(TreasuryError):
    """Raised when a spend exceeds the available balance of an envelope."""


class ReserveLockedError(TreasuryError):
    """Raised when something tries to spend from a locked envelope.

    This is the guard that makes the 80% reserve real. The agent can request
    any spend it likes; the treasury refuses the ones that touch reserve.
    """


class SpendLimitExceededError(TreasuryError):
    """Raised when a spend breaches a configured circuit breaker."""


class UnbalancedEntryError(TreasuryError):
    """Raised when an entry's debits and credits do not reconcile."""


class ReservationError(TreasuryError):
    """Raised when a reservation is unknown, expired, or already settled."""


class LedgerClosedError(TreasuryError):
    """Raised when a write is attempted on a closed ledger."""
