"""A persistent crawl frontier, so an overnight job can be interrupted.

A frontier in memory is useless for a job that runs for hours: if the process
dies at 03:00, an in-memory queue takes the remaining work with it. This is a
SQLite table instead, so the next run resumes exactly where the last one
stopped, and a host that failed repeatedly is remembered rather than retried
forever.

State per URL is deliberately small. A crawler that stores a great deal about
each URL tends to accumulate bookkeeping faster than it accumulates content.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from harvest.errors import HarvestError

PENDING = "pending"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS harvest_frontier (
        url TEXT PRIMARY KEY,
        host TEXT NOT NULL,
        depth INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        discovered_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS harvest_hosts (
        host TEXT PRIMARY KEY,
        failure_count INTEGER NOT NULL DEFAULT 0,
        disabled_until TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS harvest_log (
        entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        event TEXT NOT NULL,
        url TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_frontier_state
    ON harvest_frontier (state, discovered_at)
    """,
)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_HOST_COOLDOWN_MINUTES = 30.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class FrontierItem:
    url: str
    host: str
    depth: int
    attempts: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "host": self.host,
            "depth": self.depth,
            "attempts": self.attempts,
        }


class Frontier:
    """Durable, bounded URL queue with per-host backoff."""

    def __init__(
        self,
        database_path: str | Path = "data/harvest.db",
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        host_cooldown_minutes: float = DEFAULT_HOST_COOLDOWN_MINUTES,
        timeout: float = 5.0,
    ) -> None:
        memory = str(database_path) == ":memory:"
        self.database_path = Path(":memory:") if memory else Path(database_path)
        if not memory:
            try:
                self.database_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HarvestError(
                    f"unable to create database directory: {self.database_path.parent}"
                ) from exc
        self.max_attempts = max(1, max_attempts)
        self.host_cooldown = timedelta(minutes=max(0.0, host_cooldown_minutes))
        try:
            self._connection: sqlite3.Connection | None = sqlite3.connect(
                str(self.database_path), timeout=timeout, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise HarvestError("unable to open harvest database") from exc
        self._connection.row_factory = sqlite3.Row
        try:
            self.initialize()
        except HarvestError:
            self.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise HarvestError("harvest frontier is closed")
        return self._connection

    def initialize(self) -> None:
        try:
            with self.connection:
                for statement in SCHEMA:
                    self.connection.execute(statement)
        except sqlite3.Error as exc:
            raise HarvestError("unable to initialize harvest schema") from exc

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- queue ------------------------------------------------------------

    def add(self, urls: Iterable[str], depth: int = 0) -> int:
        """Enqueue URLs, ignoring any already known."""
        from urllib.parse import urlsplit

        added = 0
        try:
            with self.connection:
                for url in urls:
                    parts = urlsplit(url)
                    if parts.scheme not in ("http", "https") or not parts.netloc:
                        continue
                    cursor = self.connection.execute(
                        """
                        INSERT OR IGNORE INTO harvest_frontier
                            (url, host, depth, state, attempts, last_error,
                             discovered_at, updated_at)
                        VALUES (?, ?, ?, 'pending', 0, '', ?, ?)
                        """,
                        (url, parts.netloc, depth, _now(), _now()),
                    )
                    added += cursor.rowcount
        except sqlite3.Error as exc:
            raise HarvestError("unable to enqueue urls") from exc
        return added

    def _host_disabled(self, host: str) -> bool:
        row = self.connection.execute(
            "SELECT disabled_until FROM harvest_hosts WHERE host = ?", (host,)
        ).fetchone()
        if row is None or not row["disabled_until"]:
            return False
        try:
            until = datetime.fromisoformat(row["disabled_until"])
        except ValueError:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        return until > datetime.now(UTC)

    def take(
        self, limit: int = 1, *, exclude_hosts: Iterable[str] = ()
    ) -> list[FrontierItem]:
        """Claim up to `limit` pending URLs, skipping backed-off hosts.

        URLs whose attempts are exhausted are first moved to `failed` with a
        reason. Leaving them `pending` would be a lie: they can never be
        claimed again, so a `pending` count would overstate the work still
        outstanding and the run would look resumable when it is not.

        A URL is re-claimable until its attempts run out, which is what makes
        an interrupted run retry rather than abandon. The worker claims once
        per cycle and marks each URL as it goes, so nothing is processed twice
        within a single pass.
        """
        blocked = set(exclude_hosts)
        claimed: list[FrontierItem] = []
        try:
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE harvest_frontier
                    SET state = 'failed',
                        last_error = 'exhausted ' || ? || ' attempt(s)',
                        updated_at = ?
                    WHERE state = 'pending' AND attempts >= ?
                    """,
                    (str(self.max_attempts), _now(), self.max_attempts),
                )
                rows = self.connection.execute(
                    """
                    SELECT url, host, depth, attempts FROM harvest_frontier
                    WHERE state = 'pending' AND attempts < ?
                    ORDER BY depth ASC, discovered_at ASC
                    """,
                    (self.max_attempts,),
                ).fetchall()
                for row in rows:
                    if len(claimed) >= limit:
                        break
                    host = row["host"]
                    if host in blocked or self._host_disabled(host):
                        continue
                    self.connection.execute(
                        """
                        UPDATE harvest_frontier
                        SET attempts = attempts + 1, updated_at = ?
                        WHERE url = ?
                        """,
                        (_now(), row["url"]),
                    )
                    claimed.append(
                        FrontierItem(
                            url=row["url"],
                            host=host,
                            depth=int(row["depth"]),
                            attempts=int(row["attempts"]) + 1,
                        )
                    )
        except sqlite3.Error as exc:
            raise HarvestError("unable to claim urls") from exc
        return claimed

    def mark(self, url: str, state: str, error: str = "") -> None:
        if state not in (PENDING, DONE, FAILED, SKIPPED):
            raise HarvestError(f"unknown frontier state: {state}")
        try:
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE harvest_frontier
                    SET state = ?, last_error = ?, updated_at = ?
                    WHERE url = ?
                    """,
                    (state, error[:500], _now(), url),
                )
        except sqlite3.Error as exc:
            raise HarvestError("unable to update frontier") from exc

    def penalize_host(self, host: str) -> None:
        """Back a host off after repeated failure.

        A single unresponsive host should not consume the whole night's budget
        through retries. This is the difference between a crawler that
        finishes and one that spends twelve hours on a dead domain.
        """
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO harvest_hosts (host, failure_count, disabled_until, updated_at)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(host) DO UPDATE SET
                        failure_count = failure_count + 1,
                        disabled_until = excluded.disabled_until,
                        updated_at = excluded.updated_at
                    """,
                    (host, (_now_delta(self.host_cooldown)), _now()),
                )
        except sqlite3.Error as exc:
            raise HarvestError("unable to penalize host") from exc

    def restore(self, urls: Iterable[str]) -> int:
        """Return URLs to the pending state. Used on shutdown for in-flight work."""
        restored = 0
        try:
            with self.connection:
                for url in urls:
                    cursor = self.connection.execute(
                        """
                        UPDATE harvest_frontier
                        SET state = 'pending', updated_at = ?
                        WHERE url = ? AND state = 'pending'
                        """,
                        (_now(), url),
                    )
                    restored += cursor.rowcount
        except sqlite3.Error as exc:
            raise HarvestError("unable to restore urls") from exc
        return restored

    # -- introspection ----------------------------------------------------

    def counts(self) -> dict[str, int]:
        try:
            rows = self.connection.execute(
                "SELECT state, COUNT(*) AS n FROM harvest_frontier GROUP BY state"
            ).fetchall()
        except sqlite3.Error as exc:
            raise HarvestError("unable to count frontier") from exc
        return {row["state"]: int(row["n"]) for row in rows}

    def pending_estimate(self) -> int:
        try:
            row = self.connection.execute(
                "SELECT COUNT(*) AS n FROM harvest_frontier WHERE state = 'pending'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise HarvestError("unable to count pending") from exc
        return int(row["n"]) if row else 0

    def iter_failures(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            rows = self.connection.execute(
                """
                SELECT url, host, attempts, last_error FROM harvest_frontier
                WHERE state = 'failed' ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise HarvestError("unable to read failures") from exc
        return [dict(row) for row in rows]

    def log(self, event: str, url: str = "", detail: str = "") -> None:
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO harvest_log (at, event, url, detail) VALUES (?, ?, ?, ?)",
                    (_now(), event, url, detail[:1000]),
                )
        except sqlite3.Error as exc:
            raise HarvestError("unable to write harvest log") from exc

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            rows = self.connection.execute(
                "SELECT * FROM harvest_log ORDER BY entry_id DESC LIMIT ?", (limit,)
            ).fetchall()
        except sqlite3.Error as exc:
            raise HarvestError("unable to read harvest log") from exc
        return [dict(row) for row in rows]

    def __iter__(self) -> Iterator[FrontierItem]:
        try:
            rows = self.connection.execute(
                "SELECT url, host, depth, attempts FROM harvest_frontier"
            ).fetchall()
        except sqlite3.Error as exc:
            raise HarvestError("unable to read frontier") from exc
        for row in rows:
            yield FrontierItem(
                url=row["url"],
                host=row["host"],
                depth=int(row["depth"]),
                attempts=int(row["attempts"]),
            )

    def __repr__(self) -> str:
        return f"Frontier({str(self.database_path)!r})"


def _now_delta(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()
