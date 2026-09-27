"""The track record: the only part of this that genuinely learns.

Everything else in `revenue/` is fixed policy. This module is the feedback
signal, and it is deliberately hard to game:

* **Outcomes come from the ledger, not from the model's opinion.** An outcome
  must be recorded with money actually received, so the track record can only
  get better by being right.
* **The kill rule is one-directional.** A category's hit rate is recomputed
  from real outcomes, and a category below the floor stops being proposed.
  There is no mechanism by which a bad category becomes a good one, because
  nothing here can change the past.
* **Kill requires a minimum sample.** A single failure does not condemn a
  category. Small samples produce noise, and killing on noise is how you end
  up with a system that will only ever do the one thing that worked twice.
* **Categories are a closed set.** A model that can invent a new category per
  idea would escape its own history entirely.

The honest framing: this does not teach the agent what makes money. It stops
it from repeatedly betting on things that measurably did not pay, which is the
part of "learning to make money" that is actually achievable from a standing
start.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

from revenue.errors import RevenueError, UnknownOpportunityError
from revenue.opportunity import CATEGORIES, Opportunity, opportunity_from_dict
from treasury.money import ZERO, Money

MINOR_UNITS = 10**8

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS revenue_opportunities (
        opportunity_id TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        summary TEXT NOT NULL,
        ask_minor INTEGER NOT NULL,
        cost_minor INTEGER NOT NULL,
        currency TEXT NOT NULL,
        effort_hours REAL NOT NULL,
        horizon_days INTEGER NOT NULL,
        evidence TEXT NOT NULL DEFAULT '[]',
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS revenue_outcomes (
        opportunity_id TEXT PRIMARY KEY,
        realized_minor INTEGER NOT NULL,
        currency TEXT NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        recorded_at TEXT NOT NULL,
        FOREIGN KEY (opportunity_id)
            REFERENCES revenue_opportunities(opportunity_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS revenue_thresholds (
        category TEXT PRIMARY KEY,
        min_hit_rate TEXT NOT NULL,
        min_attempts INTEGER NOT NULL,
        max_net_loss_minor INTEGER,
        max_net_loss_currency TEXT,
        updated_at TEXT NOT NULL
    )
    """,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _to_minor(money: Money) -> int:
    return int((money.amount * MINOR_UNITS).to_integral_exact())


def _from_minor(units: int, currency: str) -> Money:
    return Money(Decimal(int(units)).scaleb(-8), currency)


@dataclass(frozen=True, slots=True)
class CategoryPolicy:
    """The bar a category must clear to keep being proposed."""

    min_hit_rate: Decimal = Decimal("0.20")
    min_attempts: int = 3
    max_net_loss: Money | None = None
    currency: str = "USD"

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_hit_rate": format(self.min_hit_rate, "f"),
            "min_attempts": self.min_attempts,
            "max_net_loss": (
                self.max_net_loss.to_dict() if self.max_net_loss is not None else None
            ),
            "currency": self.currency,
        }


@dataclass(frozen=True, slots=True)
class CategoryStats:
    """What actually happened in one category."""

    category: str
    attempts: int
    pending: int
    wins: int
    realized: Money
    projected: Money
    cost: Money
    policy: CategoryPolicy

    @property
    def hit_rate(self) -> Decimal:
        """Wins per *resolved* attempt.

        An opportunity that was evaluated but never produced an outcome is not
        an attempt. Counting evaluations here is self-sabotage: a loop that
        re-evaluates the same idea would rack up attempts with zero wins and
        kill every category, silently stopping all work. Only opportunities
        with a recorded outcome are evidence of anything.
        """
        if self.attempts <= 0:
            return ZERO
        return Decimal(self.wins) / Decimal(self.attempts)

    @property
    def net(self) -> Money:
        """Money actually received minus money actually spent.

        Not `realized - projected`. Projected is a *net* figure, so
        subtracting it from realized turns a 50 dollar loss into a 50 dollar
        gain and makes the loss gate incapable of ever firing. The cost column
        is tracked separately for exactly this reason.
        """
        return self.realized - self.cost

    @property
    def resolved(self) -> int:
        """Attempts plus still-unresolved opportunities."""
        return self.attempts + self.pending

    @property
    def killed(self) -> bool:
        """True when enough *resolved* data exists and the category fails."""
        if self.attempts < self.policy.min_attempts:
            return False
        if self.hit_rate < self.policy.min_hit_rate:
            return True
        return self.policy.max_net_loss is not None and self.net <= -abs(
            self.policy.max_net_loss
        )

    @property
    def reason(self) -> str:
        if self.attempts < self.policy.min_attempts:
            return (
                f"insufficient data ({self.attempts}/"
                f"{self.policy.min_attempts} resolved; {self.pending} pending)"
            )
        if self.hit_rate < self.policy.min_hit_rate:
            return (
                f"hit rate {format(self.hit_rate, 'f')} below floor "
                f"{format(self.policy.min_hit_rate, 'f')}"
            )
        if self.policy.max_net_loss is not None and self.net <= -abs(
            self.policy.max_net_loss
        ):
            return f"cumulative net {self.net} exceeded the loss limit"
        return "healthy"

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "attempts": self.attempts,
            "pending": self.pending,
            "resolved": self.attempts + self.pending,
            "wins": self.wins,
            "hit_rate": format(self.hit_rate, "f"),
            "realized": self.realized.to_dict(),
            "projected": self.projected.to_dict(),
            "cost": self.cost.to_dict(),
            "net": self.net.to_dict(),
            "killed": self.killed,
            "reason": self.reason,
            "policy": self.policy.to_dict(),
        }


class TrackRecord:
    """Durable, append-only record of what was proposed and what paid."""

    def __init__(
        self,
        database_path: str | Path = "data/revenue.db",
        *,
        policy: CategoryPolicy | None = None,
        timeout: float = 5.0,
    ) -> None:
        memory = str(database_path) == ":memory:"
        self.database_path = Path(":memory:") if memory else Path(database_path)
        if not memory:
            try:
                self.database_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RevenueError(
                    f"unable to create database directory: {self.database_path.parent}"
                ) from exc
        self.default_policy = policy or CategoryPolicy()
        try:
            self._connection: sqlite3.Connection | None = sqlite3.connect(
                str(self.database_path), timeout=timeout, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise RevenueError("unable to open revenue database") from exc
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        try:
            self.initialize()
        except RevenueError:
            self.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RevenueError("track record is closed")
        return self._connection

    def initialize(self) -> None:
        try:
            with self.connection:
                for statement in SCHEMA:
                    self.connection.execute(statement)
        except sqlite3.Error as exc:
            raise RevenueError("unable to initialize revenue schema") from exc

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- thresholds -------------------------------------------------------

    def set_policy(self, category: str, policy: CategoryPolicy) -> CategoryPolicy:
        """Set the bar for a category, including the cumulative loss limit.

        The loss limit is persisted rather than read from the default. Leaving
        it in the default silently disabled the loss gate, which is the kind of
        bug that looks like a working feature and never fires.
        """
        if category not in CATEGORIES:
            raise RevenueError(f"unknown category: {category}")
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO revenue_thresholds
                        (category, min_hit_rate, min_attempts,
                         max_net_loss_minor, max_net_loss_currency, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        min_hit_rate = excluded.min_hit_rate,
                        min_attempts = excluded.min_attempts,
                        max_net_loss_minor = excluded.max_net_loss_minor,
                        max_net_loss_currency = excluded.max_net_loss_currency,
                        updated_at = excluded.updated_at
                    """,
                    (
                        category,
                        format(policy.min_hit_rate, "f"),
                        policy.min_attempts,
                        (
                            _to_minor(policy.max_net_loss)
                            if policy.max_net_loss is not None
                            else None
                        ),
                        (
                            policy.max_net_loss.currency
                            if policy.max_net_loss is not None
                            else None
                        ),
                        _now(),
                    ),
                )
        except sqlite3.Error as exc:
            raise RevenueError("unable to store category policy") from exc
        return policy

    def policy_for(self, category: str) -> CategoryPolicy:
        if category not in CATEGORIES:
            raise RevenueError(f"unknown category: {category}")
        try:
            row = self.connection.execute(
                "SELECT * FROM revenue_thresholds WHERE category = ?", (category,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise RevenueError("unable to read category policy") from exc
        if row is None:
            return self.default_policy
        loss = None
        if row["max_net_loss_minor"] is not None:
            loss = _from_minor(
                row["max_net_loss_minor"],
                row["max_net_loss_currency"] or self.default_policy.currency,
            )
        return CategoryPolicy(
            min_hit_rate=Decimal(row["min_hit_rate"]),
            min_attempts=int(row["min_attempts"]),
            max_net_loss=loss,
        )

    # -- writing ----------------------------------------------------------

    def record_opportunity(self, opportunity: Opportunity) -> str:
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO revenue_opportunities
                        (opportunity_id, category, summary, ask_minor, cost_minor,
                         currency, effort_hours, horizon_days, evidence, payload, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        opportunity.opportunity_id,
                        opportunity.category,
                        opportunity.summary,
                        _to_minor(opportunity.ask_price),
                        _to_minor(opportunity.expected_cost),
                        opportunity.currency,
                        opportunity.effort_hours,
                        opportunity.horizon_days,
                        json.dumps(
                            [item.to_dict() for item in opportunity.evidence],
                            sort_keys=True,
                        ),
                        json.dumps(opportunity.to_dict(), sort_keys=True),
                        opportunity.created_at,
                    ),
                )
        except sqlite3.Error as exc:
            raise RevenueError("unable to record opportunity") from exc
        return opportunity.opportunity_id

    def record_outcome(
        self,
        opportunity_id: str,
        realized: Money,
        *,
        notes: str = "",
    ) -> str:
        """Record money actually received. Zero is a legitimate outcome."""
        if not self.has_opportunity(opportunity_id):
            raise UnknownOpportunityError(
                f"opportunity was never recorded: {opportunity_id}"
            )
        if realized.is_negative:
            raise RevenueError("realized revenue must not be negative")
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO revenue_outcomes
                        (opportunity_id, realized_minor, currency, notes, recorded_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        opportunity_id,
                        _to_minor(realized),
                        realized.currency,
                        notes,
                        _now(),
                    ),
                )
        except sqlite3.Error as exc:
            raise RevenueError("unable to record outcome") from exc
        return opportunity_id

    # -- reading ----------------------------------------------------------

    def has_opportunity(self, opportunity_id: str) -> bool:
        try:
            row = self.connection.execute(
                "SELECT 1 FROM revenue_opportunities WHERE opportunity_id = ?",
                (opportunity_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise RevenueError("unable to query opportunities") from exc
        return row is not None

    def get_opportunity(self, opportunity_id: str) -> Opportunity:
        try:
            row = self.connection.execute(
                "SELECT payload FROM revenue_opportunities WHERE opportunity_id = ?",
                (opportunity_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise RevenueError("unable to read opportunity") from exc
        if row is None:
            raise UnknownOpportunityError(
                f"opportunity was never recorded: {opportunity_id}"
            )
        return opportunity_from_dict(json.loads(row["payload"]))

    def category_stats(self, category: str) -> CategoryStats:
        if category not in CATEGORIES:
            raise RevenueError(f"unknown category: {category}")
        try:
            rows = self.connection.execute(
                """
                SELECT o.ask_minor AS ask_minor,
                       o.cost_minor AS cost_minor,
                       o.currency AS currency,
                       r.realized_minor AS realized_minor
                FROM revenue_opportunities AS o
                LEFT JOIN revenue_outcomes AS r
                    ON r.opportunity_id = o.opportunity_id
                WHERE o.category = ?
                """,
                (category,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise RevenueError("unable to read category statistics") from exc

        currency = rows[0]["currency"] if rows else "USD"
        attempts = 0
        wins = 0
        pending = 0
        realized = ZERO
        projected = ZERO
        cost_total = ZERO
        for row in rows:
            ask = _from_minor(row["ask_minor"], row["currency"])
            cost = _from_minor(row["cost_minor"], row["currency"])
            projected = projected + (ask - cost).amount
            cost_total = cost_total + cost.amount
            if row["realized_minor"] is None:
                # Recorded but unresolved. Deliberately not counted as an
                # attempt, so that re-evaluating an idea cannot manufacture
                # losses and kill a healthy category.
                pending += 1
                continue
            # Resolved: this one is a real attempt.
            attempts += 1
            received = _from_minor(row["realized_minor"], row["currency"])
            realized = realized + received.amount
            # A "win" is clearing the ask price, not merely not losing money.
            # Anything less is a miss, which is what the floor should measure.
            if received.amount >= ask.amount:
                wins += 1
        return CategoryStats(
            category=category,
            attempts=attempts,
            pending=pending,
            wins=wins,
            realized=_from_minor(int(realized.scaleb(8)), currency),
            projected=_from_minor(int(projected.scaleb(8)), currency),
            cost=_from_minor(int(cost_total.scaleb(8)), currency),
            policy=self.policy_for(category),
        )

    def all_stats(self) -> list[CategoryStats]:
        return [self.category_stats(category) for category in CATEGORIES]

    def allowed_categories(self) -> tuple[str, ...]:
        """Categories still eligible to be proposed."""
        return tuple(stats.category for stats in self.all_stats() if not stats.killed)

    def blocked_categories(self) -> dict[str, str]:
        """Killed categories and the measured reason each one died."""
        return {
            stats.category: stats.reason for stats in self.all_stats() if stats.killed
        }

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            rows = self.connection.execute(
                """
                SELECT o.opportunity_id, o.category, o.summary, o.ask_minor,
                       o.cost_minor, o.currency, o.created_at,
                       r.realized_minor, r.recorded_at
                FROM revenue_opportunities AS o
                LEFT JOIN revenue_outcomes AS r
                    ON r.opportunity_id = o.opportunity_id
                ORDER BY o.created_at DESC LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        except sqlite3.Error as exc:
            raise RevenueError("unable to read history") from exc
        history: list[dict[str, Any]] = []
        for row in rows:
            ask = _from_minor(row["ask_minor"], row["currency"])
            cost = _from_minor(row["cost_minor"], row["currency"])
            history.append(
                {
                    "opportunity_id": row["opportunity_id"],
                    "category": row["category"],
                    "summary": row["summary"],
                    "ask": ask.to_dict(),
                    "net": (ask - cost).to_dict(),
                    "created_at": row["created_at"],
                    "realized": (
                        _from_minor(row["realized_minor"], row["currency"]).to_dict()
                        if row["realized_minor"] is not None
                        else None
                    ),
                    "recorded_at": row["recorded_at"],
                }
            )
        return history

    def __repr__(self) -> str:
        return f"TrackRecord({str(self.database_path)!r})"


def policy_from_dict(payload: Mapping[str, Any]) -> CategoryPolicy:
    return CategoryPolicy(
        min_hit_rate=Decimal(str(payload.get("min_hit_rate", "0.20"))),
        min_attempts=int(payload.get("min_attempts", 3)),
        max_net_loss=(
            Money.from_dict(payload["max_net_loss"])
            if payload.get("max_net_loss")
            else None
        ),
    )
