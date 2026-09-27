"""The treasury facade: earn, spend, and release, with the rules attached.

This is the object the agent loop talks to. It is intentionally small. The
interesting decisions live in `policy` (what is allowed) and `ledger` (what is
true); this module only sequences them so that no caller can reach the spend
path without passing the policy check first.

The three verbs map to the three real-world flows:

* `earn`  - money arrives from the outside world. One entry, three legs:
            debit `WORLD`, credit `OPERATE` with 20%, credit `RESERVE` with
            80%. Because it is a single balanced entry, the split can never be
            recorded inconsistently.
* `spend` - the agent pays for something. Refused outright if it names a
            locked envelope or breaches a configured limit.
* `release_reserve` - the owner moves locked money into the spendable
            envelope. Separate method, separate authorization argument, so the
            agent's own spend path has no route to it.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Self

from core.errors import DatabaseError
from treasury.errors import (
    CurrencyMismatchError,
    InsufficientFundsError,
    InvalidAmountError,
    ReservationError,
    ReserveLockedError,
    UnknownEnvelopeError,
)
from treasury.ledger import (
    ENVELOPE,
    EXTERNAL,
    SINK,
    WORLD,
    Direction,
    Entry,
    EntryKind,
    Ledger,
    Posting,
    _from_minor,
    _to_minor,
)
from treasury.money import Money
from treasury.policy import (
    OPERATE,
    RESERVE,
    Allocation,
    BudgetPolicy,
    Envelope,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class Run:
    """One autonomous run, with its own spend accounting."""

    run_id: str
    currency: str
    started_at: str
    income: Money
    spent: Money
    reserved: Money
    finished_at: str | None = None
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "currency": self.currency,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "income": self.income.to_dict(),
            "spent": self.spent.to_dict(),
            "reserved": self.reserved.to_dict(),
            "metadata": dict(self.metadata or {}),
        }


class Treasury:
    """A self-funding account whose reserve the agent cannot reach."""

    def __init__(
        self,
        database_path: str | Path = "data/treasury.db",
        *,
        policy: BudgetPolicy | None = None,
    ) -> None:
        self.policy = policy or BudgetPolicy.standard()
        self.ledger = Ledger(database_path)
        self._bootstrap_accounts()

    def _bootstrap_accounts(self) -> None:
        currency = self.policy.currency
        self.ledger.open_account(WORLD, kind=EXTERNAL, currency=currency)
        self.ledger.open_account(SINK, kind=EXTERNAL, currency=currency)
        for envelope in self.policy.envelopes:
            self.ledger.open_account(
                envelope.account_id,
                kind=ENVELOPE,
                currency=currency,
                envelope=envelope.name,
            )

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self.ledger.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- earning ----------------------------------------------------------

    def earn(
        self,
        amount: Money,
        *,
        memo: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> Allocation:
        """Record income and split it 20/80 in a single balanced entry."""
        allocation = self.policy.allocate(amount)
        entry = Entry(
            kind=EntryKind.INCOME,
            memo=memo,
            postings=(
                Posting(WORLD, Direction.DEBIT, allocation.income),
                Posting(
                    self.policy.operate.account_id,
                    Direction.CREDIT,
                    allocation.spendable,
                ),
                Posting(
                    self.policy.reserve.account_id,
                    Direction.CREDIT,
                    allocation.reserve,
                ),
            ),
            metadata=dict(metadata or {}),
        )
        self.ledger.post(entry)
        self.ledger.assert_balanced(self.policy.currency)
        return allocation

    # -- spending ---------------------------------------------------------

    def spend(
        self,
        amount: Money,
        *,
        memo: str,
        envelope_name: str = OPERATE,
        income_to_date: Money | None = None,
        spent_this_run: Money | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Entry:
        """Pay for something out of a spendable envelope.

        Raises rather than returning a falsy value, because a caller that
        ignores the refusal is exactly the failure mode a budget exists to
        prevent.
        """
        envelope = self.policy.envelope(envelope_name)
        authorization = self.policy.authorize_spend(
            envelope_name,
            amount,
            income_to_date=income_to_date,
            spent_this_run=spent_this_run,
            memo=memo,
            metadata=metadata,
        )
        entry = self._pay(envelope, amount, memo, authorization.token, metadata)
        self.ledger.assert_balanced(self.policy.currency)
        return entry

    def _pay(
        self,
        envelope: Envelope,
        amount: Money,
        memo: str,
        token: str,
        metadata: Mapping[str, Any] | None,
    ) -> Entry:
        available = self.ledger.available(envelope.account_id)
        if amount > available:
            raise InsufficientFundsError(
                f"spend of {amount} exceeds {available} available in {envelope.name}"
            )
        payload = dict(metadata or {})
        payload["authorization"] = token
        entry = Entry(
            kind=EntryKind.EXPENSE,
            memo=memo,
            postings=(
                Posting(envelope.account_id, Direction.DEBIT, amount),
                Posting(SINK, Direction.CREDIT, amount),
            ),
            metadata=payload,
        )
        return self.ledger.post(entry)

    def can_spend(self, amount: Money, *, envelope_name: str = OPERATE) -> bool:
        """Non-raising probe, for planning before committing."""
        try:
            self.policy.authorize_spend(envelope_name, amount)
        except (ReserveLockedError, InvalidAmountError, UnknownEnvelopeError):
            return False
        return amount <= self.ledger.available(
            self.policy.envelope(envelope_name).account_id
        )

    # -- reserve release (owner only) -------------------------------------

    def release_reserve(
        self,
        amount: Money,
        *,
        memo: str,
        authorization: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> Entry:
        """Move locked reserve into the spendable envelope.

        `authorization` is required and must be non-empty. This is the only
        path out of the reserve and it is intentionally awkward to call by
        accident: a separate method, and a caller-supplied proof string.
        """
        if not isinstance(authorization, str) or not authorization.strip():
            raise InvalidAmountError(
                "releasing reserve requires an explicit owner authorization string"
            )
        if not amount.is_positive:
            raise InvalidAmountError("a release must be a positive amount")
        reserve = self.policy.reserve
        operate = self.policy.operate
        available = self.ledger.available(reserve.account_id)
        if amount > available:
            raise InsufficientFundsError(
                f"release of {amount} exceeds {available} available in reserve"
            )
        payload = dict(metadata or {})
        payload["owner_authorization"] = authorization
        entry = Entry(
            kind=EntryKind.RELEASE,
            memo=memo,
            postings=(
                Posting(reserve.account_id, Direction.DEBIT, amount),
                Posting(operate.account_id, Direction.CREDIT, amount),
            ),
            metadata=payload,
        )
        self.ledger.post(entry)
        self.ledger.assert_balanced(self.policy.currency)
        return entry

    # -- runs -------------------------------------------------------------

    def start_run(self, *, metadata: Mapping[str, Any] | None = None) -> Run:
        run = Run(
            run_id=_new_id(),
            currency=self.policy.currency,
            started_at=_now(),
            income=Money.zero(self.policy.currency),
            spent=Money.zero(self.policy.currency),
            reserved=Money.zero(self.policy.currency),
            metadata=dict(metadata or {}),
        )
        try:
            with self.ledger.connection:
                self.ledger.connection.execute(
                    """
                    INSERT INTO treasury_runs
                        (run_id, started_at, income_minor, spent_minor,
                         reserved_minor, currency, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.run_id,
                        run.started_at,
                        _to_minor(run.income),
                        _to_minor(run.spent),
                        _to_minor(run.reserved),
                        run.currency,
                        json.dumps(dict(run.metadata), sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise DatabaseError("unable to start run") from exc
        return run

    def finish_run(self, run: Run, *, metadata: Mapping[str, Any] | None = None) -> Run:
        """Close a run, reporting the totals actually recorded against it.

        The totals are re-read from storage rather than copied off the `Run`
        handed in, because that object is the snapshot from `start_run` and
        carries zeroes. Trusting it would report every run as having earned
        and spent nothing.
        """
        current = self.get_run(run.run_id)
        payload = dict(current.metadata or {})
        payload.update(dict(metadata or {}))
        finished_at = _now()
        try:
            with self.ledger.connection:
                self.ledger.connection.execute(
                    """
                    UPDATE treasury_runs
                    SET finished_at = ?, metadata = ?
                    WHERE run_id = ?
                    """,
                    (finished_at, json.dumps(payload, sort_keys=True), run.run_id),
                )
        except sqlite3.Error as exc:
            raise DatabaseError("unable to finish run") from exc
        return Run(
            run_id=current.run_id,
            currency=current.currency,
            started_at=current.started_at,
            finished_at=finished_at,
            income=current.income,
            spent=current.spent,
            reserved=current.reserved,
            metadata=payload,
        )

    def get_run(self, run_id: str) -> Run:
        try:
            row = self.ledger.connection.execute(
                "SELECT * FROM treasury_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to read run") from exc
        if row is None:
            raise ReservationError(f"unknown run: {run_id}")
        return Run(
            run_id=row["run_id"],
            currency=row["currency"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            income=_from_minor(row["income_minor"], row["currency"]),
            spent=_from_minor(row["spent_minor"], row["currency"]),
            reserved=_from_minor(row["reserved_minor"], row["currency"]),
            metadata=json.loads(row["metadata"]),
        )

    _RUN_COLUMNS: ClassVar[dict[str, str]] = {
        "income": "income_minor",
        "spent": "spent_minor",
        "reserved": "reserved_minor",
    }

    def _accumulate(self, run_id: str, column: str, amount: Money) -> None:
        """Add to a run total using exact integer arithmetic.

        The addition happens in Python, not in SQL. A SQL-side
        `CAST(... AS NUMERIC)` round-trip would reintroduce the float
        rounding that the integer columns exist to avoid.
        """
        target = self._RUN_COLUMNS.get(column)
        if target is None:
            raise InvalidAmountError(f"cannot accumulate unknown column {column}")
        current = self.get_run(run_id)
        existing = getattr(current, column)
        updated = existing + amount
        try:
            with self.ledger.connection:
                self.ledger.connection.execute(
                    f"UPDATE treasury_runs SET {target} = ? WHERE run_id = ?",
                    (_to_minor(updated), run_id),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(f"unable to accumulate {column}") from exc

    # -- observations (the budgeted learning loop) -----------------------

    def record_observation(
        self,
        *,
        source: str,
        uri: str,
        title: str = "",
        summary: str = "",
        cost: Money | None = None,
        run_id: str | None = None,
    ) -> str:
        """Log something the agent learned, and what it cost to learn it.

        Costs are recorded against `OPERATE` when supplied, so research is
        budget-metered by the same rules as any other spend. Learning is not
        free, and the ledger is where that becomes visible.
        """
        if not isinstance(source, str) or not source.strip():
            raise InvalidAmountError("observation source must be a non-empty string")
        if not isinstance(uri, str) or not uri.strip():
            raise InvalidAmountError("observation uri must be a non-empty string")
        charge = cost or Money.zero(self.policy.currency)
        if charge.currency != self.policy.currency:
            raise CurrencyMismatchError(
                f"observation cost must be in {self.policy.currency}"
            )
        observation_id = _new_id()
        try:
            with self.ledger.connection:
                self.ledger.connection.execute(
                    """
                    INSERT INTO treasury_observations
                        (observation_id, run_id, source, uri, title, summary,
                         cost_minor, currency, observed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation_id,
                        run_id,
                        source.strip(),
                        uri.strip(),
                        title,
                        summary,
                        _to_minor(charge),
                        self.policy.currency,
                        _now(),
                    ),
                )
        except sqlite3.Error as exc:
            raise DatabaseError("unable to record observation") from exc
        if charge.is_positive:
            self.spend(charge, memo=f"observation: {uri.strip()}")
            if run_id is not None:
                self._accumulate(run_id, "spent", charge)
        return observation_id

    def observations(self, run_id: str | None = None) -> list[dict[str, Any]]:
        try:
            if run_id is None:
                rows = self.ledger.connection.execute(
                    "SELECT * FROM treasury_observations ORDER BY observed_at"
                ).fetchall()
            else:
                rows = self.ledger.connection.execute(
                    """
                    SELECT * FROM treasury_observations
                    WHERE run_id = ? ORDER BY observed_at
                    """,
                    (run_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to read observations") from exc
        observations = []
        for row in rows:
            record = dict(row)
            record["cost"] = format(
                _from_minor(row["cost_minor"], row["currency"]).amount, "f"
            )
            observations.append(record)
        return observations

    def research_cost(self, run_id: str | None = None) -> Money:
        """Total spent on learning. Sums exact integers, not SQL floats."""
        try:
            if run_id is None:
                row = self.ledger.connection.execute(
                    "SELECT COALESCE(SUM(cost_minor), 0) AS total "
                    "FROM treasury_observations"
                ).fetchone()
            else:
                row = self.ledger.connection.execute(
                    "SELECT COALESCE(SUM(cost_minor), 0) AS total "
                    "FROM treasury_observations WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to total research cost") from exc
        total = row["total"] or 0 if row is not None else 0
        return _from_minor(total, self.policy.currency)

    # -- reporting --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        currency = self.policy.currency
        return {
            "policy": self.policy.to_dict(),
            "ledger": self.ledger.snapshot(currency),
            "spendable": self.ledger.balance(self.policy.operate.account_id).to_dict(),
            "reserve": self.ledger.balance(self.policy.reserve.account_id).to_dict(),
            "reserve_locked": self.policy.is_locked(RESERVE),
        }

    def __repr__(self) -> str:
        return (
            f"Treasury(currency={self.policy.currency!r}, "
            f"spend_ratio={format(self.policy.spend_ratio, 'f')!r})"
        )
