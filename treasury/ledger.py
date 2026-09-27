"""Append-only double-entry ledger for the AEGIS-X treasury.

Money is recorded as postings, never as a mutable balance field. There is no
code path in this module that updates a stored total, so a bug cannot quietly
create or destroy funds: the balance of an account is always derived by
summing its postings, and a corrupted or hand-edited database is detectable
rather than invisible.

The invariant that makes the whole thing trustworthy is:

    sum(credits) - sum(debits) == 0, per currency, over the whole ledger

Every entry must satisfy it on its own, and `Ledger.assert_balanced` re-checks
it globally. The 20/80 allocation is expressed as one entry with three
postings, which is why it cannot end up 20/80 of something else.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from core.errors import DatabaseError
from treasury.errors import (
    CurrencyMismatchError,
    InvalidAmountError,
    LedgerClosedError,
    ReservationError,
    UnbalancedEntryError,
    UnknownAccountError,
)
from treasury.money import DECIMALS, ZERO, Money

EXTERNAL = "external"
ENVELOPE = "envelope"

WORLD = "ext:world"
SINK = "ext:sink"

# Amounts are persisted as scaled integers, never as TEXT and never as REAL.
#
# This is not a micro-optimization. `SUM()` over a TEXT column makes SQLite
# coerce to floating point, so balances drift by roughly 1e-14 and the ledger
# stops reconciling against exact Decimal arithmetic. `SUM()` over INTEGER is
# exact, so `sum(credits) - sum(debits) == 0` holds to the last unit no matter
# how many entries accumulate.
MINOR_UNITS = 10**DECIMALS


def _to_minor(money: Money) -> int:
    """Exact conversion to a scaled integer. The quantum divides evenly."""
    return int((money.amount * MINOR_UNITS).to_integral_exact())


def _from_minor(units: int, currency: str) -> Money:
    """Exact conversion back from a scaled integer."""
    return Money(Decimal(int(units)).scaleb(-DECIMALS), currency)


MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS treasury_accounts (
        account_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('external', 'envelope')),
        envelope TEXT,
        currency TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS treasury_entries (
        entry_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        memo TEXT NOT NULL,
        created_at TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS treasury_postings (
        posting_id TEXT PRIMARY KEY,
        entry_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        direction TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
        amount_minor INTEGER NOT NULL,
        currency TEXT NOT NULL,
        seq INTEGER NOT NULL,
        FOREIGN KEY (entry_id) REFERENCES treasury_entries(entry_id),
        FOREIGN KEY (account_id) REFERENCES treasury_accounts(account_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_postings_account
    ON treasury_postings (account_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_postings_entry
    ON treasury_postings (entry_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS treasury_reservations (
        reservation_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        amount_minor INTEGER NOT NULL,
        currency TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('held', 'settled', 'released')),
        memo TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        settled_at TEXT,
        FOREIGN KEY (account_id) REFERENCES treasury_accounts(account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS treasury_runs (
        run_id TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        income_minor INTEGER NOT NULL DEFAULT 0,
        spent_minor INTEGER NOT NULL DEFAULT 0,
        reserved_minor INTEGER NOT NULL DEFAULT 0,
        currency TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS treasury_observations (
        observation_id TEXT PRIMARY KEY,
        run_id TEXT,
        source TEXT NOT NULL,
        uri TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        cost_minor INTEGER NOT NULL DEFAULT 0,
        currency TEXT NOT NULL,
        observed_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_observations_run
    ON treasury_observations (run_id)
    """,
)


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class EntryKind(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"
    RELEASE = "release"
    ADJUSTMENT = "adjustment"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    kind: str
    currency: str
    envelope: str | None = None
    created_at: str = field(default_factory=_now)

    @property
    def is_envelope(self) -> bool:
        return self.kind == ENVELOPE

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "kind": self.kind,
            "envelope": self.envelope,
            "currency": self.currency,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class Posting:
    account_id: str
    direction: Direction
    amount: Money
    posting_id: str = field(default_factory=_new_id)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "posting_id": self.posting_id,
            "account_id": self.account_id,
            "direction": Direction(self.direction).value,
            "amount": format(self.amount.amount, "f"),
            "currency": self.amount.currency,
        }


@dataclass(frozen=True, slots=True)
class Entry:
    kind: EntryKind
    memo: str
    postings: tuple[Posting, ...]
    entry_id: str = field(default_factory=_new_id)
    created_at: str = field(default_factory=_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "kind": EntryKind(self.kind).value,
            "memo": self.memo,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "postings": [posting.to_dict() for posting in self.postings],
        }


class Ledger:
    """A durable, append-only double-entry ledger."""

    def __init__(self, database_path: str | Path, *, timeout: float = 5.0) -> None:
        memory = str(database_path) == ":memory:"
        self.database_path = Path(":memory:") if memory else Path(database_path)
        if not memory:
            try:
                self.database_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DatabaseError(
                    f"unable to create database directory: {self.database_path.parent}"
                ) from exc
        self._connection: sqlite3.Connection | None
        try:
            self._connection = sqlite3.connect(
                str(self.database_path), timeout=timeout, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise DatabaseError("unable to open treasury database") from exc
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        try:
            self.initialize()
        except DatabaseError:
            self.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise LedgerClosedError("treasury ledger is closed")
        return self._connection

    def initialize(self) -> None:
        try:
            with self.connection:
                for statement in MIGRATIONS:
                    self.connection.execute(statement)
        except sqlite3.Error as exc:
            raise DatabaseError("unable to initialize treasury schema") from exc

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- accounts ---------------------------------------------------------

    def open_account(
        self,
        account_id: str,
        *,
        kind: str,
        currency: str,
        envelope: str | None = None,
    ) -> Account:
        if not isinstance(account_id, str) or not account_id.strip():
            raise InvalidAmountError("account_id must be a non-empty string")
        if kind not in (EXTERNAL, ENVELOPE):
            raise InvalidAmountError(f"kind must be {EXTERNAL!r} or {ENVELOPE!r}")
        if kind == ENVELOPE and not envelope:
            raise InvalidAmountError("envelope accounts require an envelope name")
        account = Account(
            account_id=account_id.strip(),
            kind=kind,
            currency=currency,
            envelope=envelope,
        )
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO treasury_accounts
                        (account_id, kind, envelope, currency, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        account.account_id,
                        account.kind,
                        account.envelope,
                        account.currency,
                        account.created_at,
                    ),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(f"unable to open account {account.account_id}") from exc
        return account

    def get_account(self, account_id: str) -> Account:
        try:
            row = self.connection.execute(
                "SELECT * FROM treasury_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to read account") from exc
        if row is None:
            raise UnknownAccountError(f"unknown account: {account_id}")
        return Account(
            account_id=row["account_id"],
            kind=row["kind"],
            currency=row["currency"],
            envelope=row["envelope"],
            created_at=row["created_at"],
        )

    def list_accounts(self) -> list[Account]:
        try:
            rows = self.connection.execute(
                "SELECT * FROM treasury_accounts ORDER BY account_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to list accounts") from exc
        return [
            Account(
                account_id=row["account_id"],
                kind=row["kind"],
                currency=row["currency"],
                envelope=row["envelope"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def account_for(self, account_id: str, currency: str) -> Account:
        """Return the account, verifying it is denominated in `currency`."""
        account = self.get_account(account_id)
        if account.currency != currency:
            raise CurrencyMismatchError(
                f"account {account_id} holds {account.currency}, not {currency}"
            )
        return account

    # -- balances ---------------------------------------------------------

    def balance(self, account_id: str) -> Money:
        account = self.get_account(account_id)
        try:
            row = self.connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN direction = 'credit' THEN amount_minor END), 0)
                    - COALESCE(SUM(CASE WHEN direction = 'debit' THEN amount_minor END), 0)
                        AS total
                FROM treasury_postings
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to compute balance") from exc
        if row is None or row["total"] is None:
            return Money.zero(account.currency)
        return _from_minor(row["total"] or 0, account.currency)

    def available(self, account_id: str) -> Money:
        """Balance minus anything currently held by an open reservation.

        `balance` performs the account existence check, so this does not need
        to repeat it.
        """
        held = self.reserved(account_id)
        return self.balance(account_id) - held

    def reserved(self, account_id: str) -> Money:
        account = self.get_account(account_id)
        try:
            row = self.connection.execute(
                """
                SELECT COALESCE(SUM(amount_minor), 0) AS total
                FROM treasury_reservations
                WHERE account_id = ? AND state = 'held'
                """,
                (account_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to compute reserved amount") from exc
        if row is None or row["total"] is None:
            return Money.zero(account.currency)
        return _from_minor(row["total"] or 0, account.currency)

    # -- entries ----------------------------------------------------------

    @staticmethod
    def _validate(entry: Entry) -> None:
        if not entry.postings:
            raise UnbalancedEntryError("an entry must contain at least one posting")
        if not entry.memo.strip():
            raise InvalidAmountError("entry memo must be a non-empty string")
        currency = entry.postings[0].amount.currency
        debits = ZERO
        credits = ZERO
        for posting in entry.postings:
            if posting.amount.currency != currency:
                raise CurrencyMismatchError(
                    f"entry mixes {currency} with {posting.amount.currency}"
                )
            if posting.amount.amount < 0:
                raise InvalidAmountError("posting amounts must not be negative")
            if Direction(posting.direction) is Direction.DEBIT:
                debits = debits + posting.amount.amount
            else:
                credits = credits + posting.amount.amount
        if debits != credits:
            raise UnbalancedEntryError(
                f"entry does not reconcile: debits {debits} vs credits {credits}"
            )

    def post(self, entry: Entry) -> Entry:
        """Append a balanced entry. Nothing here ever mutates a prior row."""
        self._validate(entry)
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO treasury_entries (entry_id, kind, memo, created_at, metadata)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        entry.entry_id,
                        EntryKind(entry.kind).value,
                        entry.memo,
                        entry.created_at,
                        json.dumps(dict(entry.metadata), sort_keys=True),
                    ),
                )
                for index, posting in enumerate(entry.postings):
                    self.connection.execute(
                        """
                        INSERT INTO treasury_postings
                            (posting_id, entry_id, account_id, direction, amount_minor, currency, seq)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            posting.posting_id,
                            entry.entry_id,
                            posting.account_id,
                            Direction(posting.direction).value,
                            _to_minor(posting.amount),
                            posting.amount.currency,
                            index,
                        ),
                    )
        except sqlite3.Error as exc:
            raise DatabaseError(f"unable to post entry: {exc}") from exc
        return entry

    def get_entry(self, entry_id: str) -> Entry:
        try:
            row = self.connection.execute(
                "SELECT * FROM treasury_entries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                raise UnknownAccountError(f"unknown entry: {entry_id}")
            postings = self.connection.execute(
                """
                SELECT * FROM treasury_postings
                WHERE entry_id = ? ORDER BY seq
                """,
                (entry_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to read entry") from exc
        return Entry(
            entry_id=row["entry_id"],
            kind=EntryKind(row["kind"]),
            memo=row["memo"],
            created_at=row["created_at"],
            metadata=json.loads(row["metadata"]),
            postings=tuple(
                Posting(
                    posting_id=posting["posting_id"],
                    account_id=posting["account_id"],
                    direction=Direction(posting["direction"]),
                    amount=_from_minor(posting["amount_minor"], posting["currency"]),
                    seq=posting["seq"],
                )
                for posting in postings
            ),
        )

    def iter_entries(self, limit: int = 100) -> list[Entry]:
        if limit <= 0:
            return []
        try:
            rows = self.connection.execute(
                "SELECT entry_id FROM treasury_entries ORDER BY created_at DESC, entry_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to list entries") from exc
        return [self.get_entry(row["entry_id"]) for row in rows]

    def iter_postings(self) -> Iterator[Posting]:
        try:
            rows = self.connection.execute(
                "SELECT * FROM treasury_postings ORDER BY seq"
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to list postings") from exc
        for row in rows:
            yield Posting(
                posting_id=row["posting_id"],
                account_id=row["account_id"],
                direction=Direction(row["direction"]),
                amount=_from_minor(row["amount_minor"], row["currency"]),
                seq=row["seq"],
            )

    # -- reservations -----------------------------------------------------

    def hold(self, account_id: str, amount: Money, *, memo: str = "") -> str:
        """Reserve funds so a concurrent spender cannot also claim them."""
        self.account_for(account_id, amount.currency)
        if not amount.is_positive:
            raise InvalidAmountError("a reservation must be a positive amount")
        available = self.available(account_id)
        if amount > available:
            raise InvalidAmountError(
                f"cannot hold {amount}; only {available} available"
            )
        reservation_id = _new_id()
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO treasury_reservations
                        (reservation_id, account_id, amount_minor, currency, state, memo, created_at)
                    VALUES (?, ?, ?, ?, 'held', ?, ?)
                    """,
                    (
                        reservation_id,
                        account_id,
                        _to_minor(amount),
                        amount.currency,
                        memo,
                        _now(),
                    ),
                )
        except sqlite3.Error as exc:
            raise DatabaseError("unable to hold funds") from exc
        return reservation_id

    def settle(self, reservation_id: str) -> str:
        try:
            with self.connection:
                cursor = self.connection.execute(
                    """
                    UPDATE treasury_reservations
                    SET state = 'settled', settled_at = ?
                    WHERE reservation_id = ? AND state = 'held'
                    """,
                    (_now(), reservation_id),
                )
        except sqlite3.Error as exc:
            raise DatabaseError("unable to settle reservation") from exc
        if cursor.rowcount == 0:
            raise ReservationError(f"reservation is not held: {reservation_id}")
        return reservation_id

    def release(self, reservation_id: str) -> str:
        try:
            with self.connection:
                cursor = self.connection.execute(
                    """
                    UPDATE treasury_reservations
                    SET state = 'released', settled_at = ?
                    WHERE reservation_id = ? AND state = 'held'
                    """,
                    (_now(), reservation_id),
                )
        except sqlite3.Error as exc:
            raise DatabaseError("unable to release reservation") from exc
        if cursor.rowcount == 0:
            raise ReservationError(f"reservation is not held: {reservation_id}")
        return reservation_id

    def get_reservation(self, reservation_id: str) -> dict[str, Any]:
        try:
            row = self.connection.execute(
                "SELECT * FROM treasury_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to read reservation") from exc
        if row is None:
            raise ReservationError(f"unknown reservation: {reservation_id}")
        return {
            "reservation_id": row["reservation_id"],
            "account_id": row["account_id"],
            "amount": format(
                _from_minor(row["amount_minor"], row["currency"]).amount, "f"
            ),
            "currency": row["currency"],
            "state": row["state"],
            "memo": row["memo"],
            "created_at": row["created_at"],
            "settled_at": row["settled_at"],
        }

    # -- integrity --------------------------------------------------------

    def net_position(self, currency: str) -> Money:
        """Sum of every account balance. Must always be exactly zero."""
        try:
            row = self.connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN direction = 'credit' THEN amount_minor END), 0)
                    - COALESCE(SUM(CASE WHEN direction = 'debit' THEN amount_minor END), 0)
                        AS net
                FROM treasury_postings
                WHERE currency = ?
                """,
                (currency,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to compute net position") from exc
        if row is None or row["net"] is None:
            return Money.zero(currency)
        return _from_minor(row["net"] or 0, currency)

    def assert_balanced(self, currency: str) -> Money:
        position = self.net_position(currency)
        if not position.is_zero:
            raise UnbalancedEntryError(
                f"ledger does not reconcile in {currency}: net {position.amount}"
            )
        return position

    def snapshot(self, currency: str) -> dict[str, Any]:
        """A read-only view suitable for logging or a status command."""
        accounts = {}
        for account in self.list_accounts():
            if account.currency != currency:
                continue
            accounts[account.account_id] = {
                "kind": account.kind,
                "envelope": account.envelope,
                "balance": format(self.balance(account.account_id).amount, "f"),
                "reserved": format(self.reserved(account.account_id).amount, "f"),
                "available": format(self.available(account.account_id).amount, "f"),
            }
        return {
            "currency": currency,
            "accounts": accounts,
            "net_position": format(self.net_position(currency).amount, "f"),
        }


def build_postings(
    legs: Sequence[tuple[str, Direction, Money]],
) -> tuple[Posting, ...]:
    """Small helper for building the leg list of an entry inline."""
    return tuple(
        Posting(account_id=account_id, direction=direction, amount=amount, seq=index)
        for index, (account_id, direction, amount) in enumerate(legs)
    )
