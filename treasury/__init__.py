"""Self-funding treasury for the AEGIS-X autonomous agent.

The agent can earn, spend, and learn, and it can do none of those things
outside a hard 20% of its own income. The remaining 80% sits in a locked
reserve that the agent's spend path cannot reach at all.

Layout:

* `treasury.money`   - exact `Decimal` money; floats are rejected outright.
* `treasury.ledger`  - append-only double-entry SQLite ledger. No stored
  balances, so funds cannot be corrupted by an update bug.
* `treasury.policy`  - the 20/80 rule, the locked reserve, and the spend
  circuit breakers.
* `treasury.treasury` - `Treasury`, the facade the agent loop calls.

Typical use:

    from treasury import Money, Treasury

    with Treasury("data/treasury.db") as bank:
        split = bank.earn(Money.parse("100.00", "USD"), memo="invoice 1042")
        assert split.spendable == Money.parse("20.00", "USD")
        assert split.reserve == Money.parse("80.00", "USD")

        bank.spend(Money.parse("1.25", "USD"), memo="model API call")
        bank.spend(Money.parse("9.00", "USD"), memo="RESERVE")  # ReserveLockedError
"""

from treasury.errors import (
    CurrencyMismatchError,
    InsufficientFundsError,
    InvalidAmountError,
    LedgerClosedError,
    ReservationError,
    ReserveLockedError,
    SpendLimitExceededError,
    TreasuryError,
    UnbalancedEntryError,
    UnknownAccountError,
    UnknownEnvelopeError,
)
from treasury.ledger import (
    Account,
    Direction,
    Entry,
    EntryKind,
    Ledger,
    Posting,
)
from treasury.money import DECIMALS, QUANTUM, ZERO, Money
from treasury.policy import (
    OPERATE,
    RESERVE,
    Allocation,
    BudgetPolicy,
    Envelope,
    SpendAuthorization,
)
from treasury.treasury import Run, Treasury

__all__ = [
    "DECIMALS",
    "OPERATE",
    "QUANTUM",
    "RESERVE",
    "ZERO",
    "Account",
    "Allocation",
    "BudgetPolicy",
    "CurrencyMismatchError",
    "Direction",
    "Entry",
    "EntryKind",
    "Envelope",
    "InsufficientFundsError",
    "InvalidAmountError",
    "Ledger",
    "LedgerClosedError",
    "Money",
    "Posting",
    "ReservationError",
    "ReserveLockedError",
    "Run",
    "SpendAuthorization",
    "SpendLimitExceededError",
    "Treasury",
    "TreasuryError",
    "UnbalancedEntryError",
    "UnknownAccountError",
    "UnknownEnvelopeError",
]
