"""The allocation and spending rules that make the 20/80 split real.

Two properties matter here, and both are enforced in code rather than trusted
to the caller's discipline:

1. **The reserve is unreachable from the spend path.** `RESERVE` is a locked
   envelope. Any spend naming it is refused with `ReserveLockedError`, no
   matter who asks. Releasing reserve money is a separate operation with a
   separate method, so there is no single call that can do both.

2. **Splits always reconcile.** `allocate` rounds the spendable bucket down
   and gives the remainder to the reserve, so
   `spendable + reserve == income` exactly, to the last of the eight decimal
   places. Rounding can therefore never mint money.

`authorize_spend` issues a bound token rather than a boolean. A token names one
envelope and one amount, so a caller cannot obtain approval for a token
purchase and then spend a data centre against it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from treasury.errors import (
    CurrencyMismatchError,
    InvalidAmountError,
    ReserveLockedError,
    SpendLimitExceededError,
    UnknownEnvelopeError,
)
from treasury.money import ONE, ZERO, Money

OPERATE = "OPERATE"
RESERVE = "RESERVE"


def _new_token() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class Envelope:
    """A named budget bucket."""

    name: str
    account_id: str
    locked: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "account_id": self.account_id,
            "locked": self.locked,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class Allocation:
    """The result of splitting one income into spendable and reserve."""

    income: Money
    spendable: Money
    reserve: Money

    @property
    def reserve_ratio(self) -> Decimal:
        if not self.income.is_positive:
            return ZERO
        return self.reserve.amount / self.income.amount

    def to_dict(self) -> dict[str, Any]:
        return {
            "income": self.income.to_dict(),
            "spendable": self.spendable.to_dict(),
            "reserve": self.reserve.to_dict(),
            "reserve_ratio": format(self.reserve_ratio, "f"),
        }


@dataclass(frozen=True, slots=True)
class SpendAuthorization:
    """Permission to move exactly `amount` out of exactly one envelope."""

    token: str
    envelope: str
    amount: Money
    memo: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "envelope": self.envelope,
            "amount": self.amount.to_dict(),
            "memo": self.memo,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """The rules governing how income is split and how it may be spent."""

    currency: str = "USD"
    spend_ratio: Decimal = Decimal("0.20")
    locked_envelopes: frozenset[str] = frozenset({RESERVE})
    envelopes: tuple[Envelope, ...] = ()
    max_spend_ratio_of_income: Decimal = ONE
    max_spend_per_run: Money | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise InvalidAmountError("policy currency must be a non-empty string")
        if not isinstance(self.spend_ratio, Decimal):
            object.__setattr__(self, "spend_ratio", Decimal(str(self.spend_ratio)))
        if self.spend_ratio < ZERO or self.spend_ratio > ONE:
            raise InvalidAmountError(
                f"spend_ratio must be between 0 and 1, got {self.spend_ratio}"
            )
        if self.max_spend_ratio_of_income < ZERO:
            raise InvalidAmountError("max_spend_ratio_of_income must not be negative")
        if not self.envelopes:
            object.__setattr__(
                self,
                "envelopes",
                (
                    Envelope(
                        name=OPERATE,
                        account_id=f"env:{OPERATE}",
                        locked=False,
                        description="Spendable by the agent.",
                    ),
                    Envelope(
                        name=RESERVE,
                        account_id=f"env:{RESERVE}",
                        locked=True,
                        description=(
                            "Untouchable by the agent. Released only by an "
                            "explicit owner action."
                        ),
                    ),
                ),
            )

    @classmethod
    def standard(cls, currency: str = "USD") -> BudgetPolicy:
        """The 20/80 default: 20% spendable, 80% locked reserve."""
        return cls(currency=currency, spend_ratio=Decimal("0.20"))

    @property
    def reserve_ratio(self) -> Decimal:
        return ONE - self.spend_ratio

    @property
    def operate(self) -> Envelope:
        return self.envelope(OPERATE)

    @property
    def reserve(self) -> Envelope:
        return self.envelope(RESERVE)

    def envelope(self, name: str) -> Envelope:
        for envelope in self.envelopes:
            if envelope.name == name:
                return envelope
        raise UnknownEnvelopeError(f"unknown envelope: {name}")

    def is_locked(self, name: str) -> bool:
        return name in self.locked_envelopes or self.envelope(name).locked

    def envelope_names(self) -> tuple[str, ...]:
        return tuple(envelope.name for envelope in self.envelopes)

    # -- allocation -------------------------------------------------------

    def allocate(self, income: Money) -> Allocation:
        """Split income into spendable and reserve, exactly reconciling.

        The spendable side is rounded down and the reserve takes the
        remainder, so the pair always sums back to the original income.
        """
        if income.currency != self.currency:
            raise CurrencyMismatchError(
                f"policy is denominated in {self.currency}, got {income.currency}"
            )
        if not income.is_positive:
            raise InvalidAmountError("income must be a positive amount")
        spendable = income.scaled(self.spend_ratio)
        reserve = income - spendable
        allocation = Allocation(income=income, spendable=spendable, reserve=reserve)
        if allocation.spendable + allocation.reserve != income:
            raise InvalidAmountError(
                "allocation failed to reconcile; refusing to record it"
            )
        return allocation

    # -- authorization ----------------------------------------------------

    def authorize_spend(
        self,
        envelope_name: str,
        amount: Money,
        *,
        income_to_date: Money | None = None,
        spent_this_run: Money | None = None,
        memo: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> SpendAuthorization:
        """Approve a spend, or refuse it. Refusal is the common case.

        This is the only door to the spend path, which is what makes the
        locked reserve meaningful.
        """
        envelope = self.envelope(envelope_name)
        if envelope.locked or envelope_name in self.locked_envelopes:
            raise ReserveLockedError(
                f"envelope {envelope_name} is locked and cannot be spent from; "
                "reserve release is a separate owner-authorized operation"
            )
        if amount.currency != self.currency:
            raise CurrencyMismatchError(
                f"policy is denominated in {self.currency}, got {amount.currency}"
            )
        if not amount.is_positive:
            raise InvalidAmountError("a spend must be a positive amount")

        if self.max_spend_per_run is not None:
            already = spent_this_run or Money.zero(self.currency)
            if already.currency != self.currency:
                raise CurrencyMismatchError("spent_this_run currency mismatch")
            if already + amount > self.max_spend_per_run:
                raise SpendLimitExceededError(
                    f"spend of {amount} would exceed the per-run limit of "
                    f"{self.max_spend_per_run} (already spent {already})"
                )

        if income_to_date is not None and income_to_date.is_positive:
            ceiling = income_to_date.scaled(self.max_spend_ratio_of_income)
            already = spent_this_run or Money.zero(self.currency)
            if already + amount > ceiling:
                raise SpendLimitExceededError(
                    f"spend of {amount} would exceed {ceiling}, the configured "
                    f"ceiling on {income_to_date} of income to date "
                    f"(already spent {already})"
                )

        return SpendAuthorization(
            token=_new_token(),
            envelope=envelope_name,
            amount=amount,
            memo=memo,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "spend_ratio": format(self.spend_ratio, "f"),
            "reserve_ratio": format(self.reserve_ratio, "f"),
            "locked_envelopes": sorted(self.locked_envelopes),
            "envelopes": [envelope.to_dict() for envelope in self.envelopes],
            "max_spend_ratio_of_income": format(self.max_spend_ratio_of_income, "f"),
            "max_spend_per_run": (
                self.max_spend_per_run.to_dict()
                if self.max_spend_per_run is not None
                else None
            ),
        }
