"""Self-improvement from measured outcomes: tune numbers, never logic.

This is the safe half of "make it improve itself". The agent cannot rewrite
its own source, and that is the correct restriction, because the code that
enforces the constraints is the same code an edit would disable. So the
improvement surface is everything *except* the logic:

* **Operational thresholds** are re-derived from what actually happened. A
  corpus that verified at 35% should be curated more strictly next time; a
  category that lost money should be proposed less; an ask price that never
  sells should come down.
* **Lessons** are written from the track record and fed back into the agent's
  own system prompt, so it stops repeating arguments that have already been
  measured and refuted.

Every adjustment is bounded, logged with before and after, and reversible. The
tuner cannot widen its own permissions, spend money, or grant a capability,
because it has no route to any of those. Tuning a number is not the same
power as changing the rules, and keeping that separation is what makes this
safe to run unattended.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, ClassVar, Self

from revenue.trackrecord import TrackRecord

# Bounds. The tuner may move a value inside its band and no further. A tuner
# that can set a threshold to anything is a tuner that can disable a gate.
BOUNDS: dict[str, tuple[Decimal, Decimal]] = {
    "curation.min_quality_score": (Decimal("0.50"), Decimal("0.95")),
    "market.viable_median_downloads": (Decimal(100), Decimal(50000)),
    "pricing.ask_multiplier": (Decimal("0.25"), Decimal("4.00")),
    "opportunity.min_margin": (Decimal("0.10"), Decimal("0.90")),
}

MAX_LESSONS = 40


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _clamp(name: str, value: Decimal) -> Decimal:
    low, high = BOUNDS.get(name, (Decimal(0), Decimal(100)))
    return max(low, min(high, value))


@dataclass(frozen=True, slots=True)
class Adjustment:
    """One proposed change to one number, with the reason and the evidence."""

    name: str
    current: Decimal
    proposed: Decimal
    reason: str
    evidence: str
    applied: bool = False
    created_at: str = field(default_factory=_now)

    @property
    def changed(self) -> bool:
        return self.current != self.proposed

    @property
    def direction(self) -> str:
        if self.proposed > self.current:
            return "up"
        return "down" if self.proposed < self.current else "flat"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "current": format(self.current, "f"),
            "proposed": format(self.proposed, "f"),
            "direction": self.direction,
            "changed": self.changed,
            "reason": self.reason,
            "evidence": self.evidence,
            "applied": self.applied,
            "created_at": self.created_at,
        }


SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS selftune_parameters (
        name TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        rationale TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS selftune_log (
        entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        name TEXT NOT NULL,
        before_value TEXT NOT NULL,
        after_value TEXT NOT NULL,
        reason TEXT NOT NULL,
        evidence TEXT NOT NULL DEFAULT '',
        applied INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS selftune_lessons (
        lesson TEXT PRIMARY KEY,
        category TEXT NOT NULL DEFAULT '',
        at TEXT NOT NULL,
        evidence TEXT NOT NULL DEFAULT ''
    )
    """,
)


class SelfTuner:
    """Derives better parameters and better instructions from outcomes.

    Reads three kinds of evidence:

    * the track record, for per-category hit rate and realized revenue
    * a verification report, for the execution rate of the shipped corpus
    * a market report, for observed demand

    Writes parameters, a change log, and lessons. Never writes code, never
    grants a capability, never spends.
    """

    DEFAULTS: ClassVar[dict[str, Decimal]] = {
        "curation.min_quality_score": Decimal("0.60"),
        "market.viable_median_downloads": Decimal(1000),
        "pricing.ask_multiplier": Decimal("1.00"),
        "opportunity.min_margin": Decimal("0.35"),
    }

    def __init__(
        self,
        database_path: str | Path = "data/selftune.db",
        *,
        track_record: TrackRecord | None = None,
        timeout: float = 5.0,
    ) -> None:
        memory = str(database_path) == ":memory:"
        self.database_path = Path(":memory:") if memory else Path(database_path)
        if not memory:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.track_record = track_record
        try:
            self._connection: sqlite3.Connection | None = sqlite3.connect(
                str(self.database_path), timeout=timeout, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise RuntimeError(f"unable to open selftune store: {exc}") from exc
        self._connection.row_factory = sqlite3.Row
        try:
            self.initialize()
        except Exception:
            self.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("selftune store is closed")
        return self._connection

    def initialize(self) -> None:
        with self.connection:
            for statement in SCHEMA:
                self.connection.execute(statement)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- parameters -------------------------------------------------------

    def get(self, name: str) -> Decimal:
        row = self.connection.execute(
            "SELECT value FROM selftune_parameters WHERE name = ?", (name,)
        ).fetchone()
        if row is not None:
            try:
                return Decimal(row["value"])
            except Exception:  # noqa: BLE001, S110 - a bad row falls to default
                # A corrupt row must not stop the agent; the default is the
                # safe value precisely because it is the un-tuned one.
                pass
        return self.DEFAULTS.get(name, Decimal(0))

    def all_parameters(self) -> dict[str, str]:
        values = {name: format(value, "f") for name, value in self.DEFAULTS.items()}
        for row in self.connection.execute(
            "SELECT name, value FROM selftune_parameters"
        ):
            values[row["name"]] = row["value"]
        return values

    def _store(self, name: str, value: Decimal, rationale: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO selftune_parameters (name, value, updated_at, rationale)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    rationale = excluded.rationale
                """,
                (name, format(value, "f"), _now(), rationale[:500]),
            )

    # -- proposing --------------------------------------------------------

    def propose(
        self,
        *,
        verification: Mapping[str, Any] | None = None,
        market: Mapping[str, Any] | None = None,
    ) -> list[Adjustment]:
        """Work out what should change, from what has been measured."""
        proposals: list[Adjustment] = []
        proposals.extend(self._from_verification(verification))
        proposals.extend(self._from_market(market))
        proposals.extend(self._from_track_record())
        proposals.extend(self._from_pricing(verification))
        return [item for item in proposals if item.changed]

    def _from_verification(
        self, verification: Mapping[str, Any] | None
    ) -> list[Adjustment]:
        if not verification:
            return []
        buckets = verification.get("buckets") or {}
        if not buckets:
            return []
        examined = int(verification.get("records_examined") or 0)
        if examined <= 0:
            return []
        unsafe = int(buckets.get("unsafe") or 0)
        failed = int(buckets.get("failed") or 0)
        rate = Decimal(str(verification.get("verified_rate_of_executable") or "0"))
        current = self.get("curation.min_quality_score")

        if rate < Decimal("0.50"):
            proposed = _clamp("curation.min_quality_score", current + Decimal("0.10"))
            return [
                Adjustment(
                    name="curation.min_quality_score",
                    current=current,
                    proposed=proposed,
                    reason=(
                        f"only {format(rate, 'f')} of executable records ran, so "
                        "the curation bar was too low for this corpus"
                    ),
                    evidence=(
                        f"{unsafe} refused as unsafe, {failed} raised on execution, "
                        f"of {examined} examined"
                    ),
                )
            ]
        if rate > Decimal("0.80") and unsafe == 0 and failed == 0:
            proposed = _clamp("curation.min_quality_score", current - Decimal("0.05"))
            return [
                Adjustment(
                    name="curation.min_quality_score",
                    current=current,
                    proposed=proposed,
                    reason=(
                        f"{format(rate, 'f')} of executable records ran cleanly "
                        "with no unsafe and no failures, so the bar can relax"
                    ),
                    evidence=f"{examined} records examined, 0 unsafe, 0 failed",
                )
            ]
        return []

    def _from_market(self, market: Mapping[str, Any] | None) -> list[Adjustment]:
        if not market:
            return []
        read = market.get("demand_read") or {}
        if "viable" not in read:
            return []
        aggregate = market.get("aggregate") or {}
        downloads = int(aggregate.get("total_downloads") or 0)
        current = self.get("market.viable_median_downloads")
        if not read.get("viable"):
            # Repeatedly dead markets mean the bar is set where nothing lives.
            # Raise it so the agent stops probing hopeless domains.
            proposed = _clamp("market.viable_median_downloads", current + Decimal(250))
            return [
                Adjustment(
                    name="market.viable_median_downloads",
                    current=current,
                    proposed=proposed,
                    reason=(
                        "the probed market showed no demand, so the viability "
                        "bar should be raised rather than the domain retried"
                    ),
                    evidence=f"{downloads} downloads observed across comparables",
                )
            ]
        proposed = _clamp(
            "market.viable_median_downloads", max(Decimal(100), current - Decimal(100))
        )
        return [
            Adjustment(
                name="market.viable_median_downloads",
                current=current,
                proposed=proposed,
                reason="the probed market cleared the bar, so it can be relaxed",
                evidence=f"{downloads} downloads observed across comparables",
            )
        ]

    def _from_track_record(self) -> list[Adjustment]:
        if self.track_record is None:
            return []
        # A category that clears its ask repeatedly is worth pushing harder,
        # so the margin floor can come down for it. A category that lost is
        # already blocked; the margin floor is global and stays put.
        healthy = [
            stats
            for stats in self.track_record.all_stats()
            if stats.attempts >= 3
            and stats.hit_rate >= Decimal("0.60")
            and not stats.killed
        ]
        if not healthy:
            return []
        current = self.get("opportunity.min_margin")
        proposed = _clamp(
            "opportunity.min_margin",
            (current - Decimal("0.05")).quantize(Decimal("0.01")),
        )
        names = ", ".join(sorted(stats.category for stats in healthy))
        return [
            Adjustment(
                name="opportunity.min_margin",
                current=current,
                proposed=proposed,
                reason=(
                    "at least one category is clearing its ask price "
                    "consistently, so thin-margin work in it is worth taking"
                ),
                evidence=(
                    "; ".join(
                        f"{stats.category} {stats.wins}/{stats.attempts}"
                        for stats in healthy
                    )
                    + f" (healthy: {names})"
                ),
            )
        ]

    def _from_pricing(self, verification: Mapping[str, Any] | None) -> list[Adjustment]:
        """Align the ask with what verification actually justifies."""
        if not verification:
            return []
        examined = int(verification.get("records_examined") or 0)
        buckets = verification.get("buckets") or {}
        usable = int(buckets.get("verified") or 0)
        if examined <= 0 or usable <= 0:
            return []
        fraction = (Decimal(usable) / Decimal(examined)).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        # Scale the ask by the fraction of the corpus that is actually
        # verified. Claiming full price for a corpus that is 30% unverified
        # is the kind of thing that loses a buyer and a reputation.
        current = self.get("pricing.ask_multiplier")
        proposed = _clamp("pricing.ask_multiplier", fraction)
        return [
            Adjustment(
                name="pricing.ask_multiplier",
                current=current,
                proposed=proposed,
                reason=(
                    f"only {format(fraction, 'f')} of the corpus is "
                    "execution-verified, so the ask is scaled to what is "
                    "actually usable"
                ),
                evidence=f"{usable} verified of {examined} examined",
            )
        ]

    # -- applying ---------------------------------------------------------

    def apply(self, adjustments: Sequence[Adjustment]) -> list[Adjustment]:
        """Commit the changes. Bounded, logged, and reversible."""
        applied: list[Adjustment] = []
        for adjustment in adjustments:
            bounded = _clamp(adjustment.name, adjustment.proposed)
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO selftune_log
                        (at, name, before_value, after_value, reason, evidence, applied)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        _now(),
                        adjustment.name,
                        format(adjustment.current, "f"),
                        format(bounded, "f"),
                        adjustment.reason,
                        adjustment.evidence[:400],
                    ),
                )
            self._store(adjustment.name, bounded, adjustment.reason)
            applied.append(
                Adjustment(
                    name=adjustment.name,
                    current=adjustment.current,
                    proposed=bounded,
                    reason=adjustment.reason,
                    evidence=adjustment.evidence,
                    applied=True,
                )
            )
        return applied

    def revert(self, name: str) -> Decimal:
        """Put a parameter back to its default and log the revert."""
        default = self.DEFAULTS.get(name, Decimal(0))
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO selftune_log
                    (at, name, before_value, after_value, reason, evidence, applied)
                VALUES (?, ?, ?, ?, 'reverted to default', '', 1)
                """,
                (_now(), name, format(self.get(name), "f"), format(default, "f")),
            )
        self._store(name, default, "reverted to default")
        return default

    def history(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM selftune_log ORDER BY entry_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- lessons ----------------------------------------------------------

    def record_lesson(
        self, lesson: str, *, category: str = "", evidence: str = ""
    ) -> None:
        text = " ".join(lesson.split())
        if not text:
            return
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO selftune_lessons (lesson, category, at, evidence)
                VALUES (?, ?, ?, ?)
                """,
                (text[:400], category, _now(), evidence[:400]),
            )
            self.connection.execute(
                """
                DELETE FROM selftune_lessons WHERE lesson NOT IN (
                    SELECT lesson FROM selftune_lessons
                    ORDER BY at DESC LIMIT ?
                )
                """,
                (MAX_LESSONS,),
            )

    def learn_from_outcomes(self) -> list[str]:
        """Derive lessons from the track record and store them.

        This is the part that feeds back into behaviour rather than numbers:
        each lesson is a sentence the agent is told about its own history, so
        it can decline to re-argue an idea that has already been measured.
        """
        if self.track_record is None:
            return []
        learned: list[str] = []
        for stats in self.track_record.all_stats():
            if stats.attempts < 2:
                continue
            if stats.killed:
                self.record_lesson(
                    f"Category '{stats.category}' was tried {stats.attempts} times "
                    f"at a hit rate of {format(stats.hit_rate, 'f')} and is not "
                    f"paying: {stats.reason}. Do not propose it again.",
                    category=stats.category,
                    evidence=stats.to_dict()["reason"],
                )
                learned.append(stats.category)
            elif stats.wins > 0:
                self.record_lesson(
                    f"Category '{stats.category}' cleared its ask price "
                    f"{stats.wins} time(s) out of {stats.attempts}; prefer it.",
                    category=stats.category,
                    evidence=f"hit rate {format(stats.hit_rate, 'f')}",
                )
                learned.append(stats.category)
        return learned

    def lessons(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM selftune_lessons ORDER BY at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def lesson_block(self, limit: int = 12) -> str:
        """A prompt-ready block of what this agent has learned about itself."""
        entries = self.lessons(limit)
        if not entries:
            return ""
        lines = [
            "What you have already measured about your own work:",
        ]
        lines += [f"- {row['lesson']}" for row in entries]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": _now(),
            "parameters": self.all_parameters(),
            "bounds": {
                name: [format(low, "f"), format(high, "f")]
                for name, (low, high) in BOUNDS.items()
            },
            "lessons": len(self.lessons(limit=MAX_LESSONS)),
            "recent_changes": self.history(10),
        }
