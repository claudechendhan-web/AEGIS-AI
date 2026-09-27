"""Exact money arithmetic for the AEGIS-X treasury.

Money is never a float. Every amount is a `Decimal` quantized to
`DECIMALS` places and carries an explicit currency, because the 20/80 rule is
only sound if the spendable bucket and the reserve always reconcile back to
the original income without losing or inventing a unit.

Two rounding modes are used deliberately, and they are not interchangeable:

* Inputs are **rejected** when they carry more precision than the quantum.
  Silently truncating a payment is how money goes missing.
* Derived splits (the 20/80 allocation) round **down** on the spendable
  bucket. Rounding the reserve down instead would let rounding hand the
  agent spendable money it was never allocated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from treasury.errors import CurrencyMismatchError, InvalidAmountError

DECIMALS = 8
QUANTUM = Decimal(1).scaleb(-DECIMALS)
ZERO = Decimal(0).quantize(QUANTUM)
ONE = Decimal(1).quantize(QUANTUM)

_MAX_CURRENCY_LENGTH = 16


def _as_decimal(value: object, name: str) -> Decimal:
    """Coerce `value` to a Decimal without ever going through float."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise InvalidAmountError(f"{name} must be a number, not a bool")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        raise InvalidAmountError(
            f"{name} must not be a float; pass a str or Decimal to keep the value exact"
        )
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise InvalidAmountError(f"{name} must not be empty")
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise InvalidAmountError(
                f"{name} is not a valid decimal: {value!r}"
            ) from exc
    raise InvalidAmountError(f"{name} must be a str, int, or Decimal")


def _exact(value: object, name: str) -> Decimal:
    """Coerce to a Decimal quantized exactly, rejecting excess precision."""
    number = _as_decimal(value, name)
    if not number.is_finite():
        raise InvalidAmountError(f"{name} must be finite")
    quantized = number.quantize(QUANTUM)
    if quantized != number:
        raise InvalidAmountError(
            f"{name} has more than {DECIMALS} decimal places: {number}"
        )
    return quantized


def _floor(value: Decimal) -> Decimal:
    """Quantize toward zero, never handing out money that was not allocated."""
    return value.quantize(QUANTUM, rounding=ROUND_DOWN)


def _normalize_currency(currency: object) -> str:
    if not isinstance(currency, str):
        raise InvalidAmountError("currency must be a string")
    code = currency.strip()
    if not code or len(code) > _MAX_CURRENCY_LENGTH:
        raise InvalidAmountError(
            f"currency must be 1 to {_MAX_CURRENCY_LENGTH} characters"
        )
    return code


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """An exact, currency-tagged amount.

    Instances are immutable and hashable so they can be used as dictionary
    keys and compared directly in tests.
    """

    amount: Decimal = ZERO
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            object.__setattr__(self, "amount", _as_decimal(self.amount, "amount"))
        exact = _exact(self.amount, "amount")
        object.__setattr__(self, "amount", exact)
        object.__setattr__(self, "currency", _normalize_currency(self.currency))

    @classmethod
    def parse(cls, value: str, currency: str) -> Money:
        """Build a Money from a decimal string such as `'19.99'`."""
        return cls(_as_decimal(value, "amount"), currency)

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(ZERO, currency)

    @classmethod
    def coerce(cls, value: Money | str | int | Decimal, currency: str) -> Money:
        if isinstance(value, Money):
            if value.currency != currency:
                raise CurrencyMismatchError(
                    f"expected {currency}, got {value.currency}"
                )
            return value
        return cls(_as_decimal(value, "amount"), currency)

    def _check(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise InvalidAmountError("operands must be Money values")
        if other.currency != self.currency:
            raise CurrencyMismatchError(
                f"cannot combine {self.currency} with {other.currency}"
            )

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    def scaled(self, ratio: Decimal | int | str) -> Money:
        """Multiply by a ratio, rounding down so the result never inflates."""
        factor = _as_decimal(ratio, "ratio")
        if not factor.is_finite():
            raise InvalidAmountError("ratio must be finite")
        return Money(_floor(self.amount * factor), self.currency)

    @property
    def is_zero(self) -> bool:
        return self.amount == ZERO

    @property
    def is_positive(self) -> bool:
        return self.amount > ZERO

    @property
    def is_negative(self) -> bool:
        return self.amount < ZERO

    def to_dict(self) -> dict[str, str]:
        return {"amount": format(self.amount, "f"), "currency": self.currency}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Money:
        return cls.parse(str(payload["amount"]), str(payload["currency"]))

    def __str__(self) -> str:
        return f"{format(self.amount, 'f')} {self.currency}"
