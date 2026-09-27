"""Revocable capability grants: the agent asks, you decide, you can change it.

This is the answer to "for every permission it asks, I should be able to
control or change it later". The existing `tools/permissions.py` decides what
a *run* may do; this decides what the agent may ever do, across runs, on disk,
and lets you take any of it back with one command.

The distinction that matters:

    tools/permissions.py   a hard gate inside one process, no memory
    security/grants.py     durable, auditable, revocable, scoped

Four decisions are built in rather than left to convention:

* **Revocation is real.** `revoke()` removes the row, and `check()` consults
  storage every time, not a cached set. A grant revoked a second ago stops
  working a second later. A cache would make revocation advisory.

* **Nothing is granted by default.** Every capability here starts absent,
  including the ones this project never auto-requests. A capability that must
  be asked for cannot be exercised by accident.

* **Scope and expiry are part of the grant, not the request.** "Read this one
  file" and "use the camera until 18:00" are different grants with different
  records. A grant wider than the task it was granted for is a bug.

* **Refusals are recorded, not silent.** `check()` writes the denial. A
  capability the agent wanted and did not get is exactly the information you
  need in order to decide whether to grant it.

On the two capabilities worth being careful about, this module takes a
position rather than just implementing plumbing:

`CAMERA` and `MICROPHONE` are implemented as **scoped, expiring, revocable
grants, never as an unconditional permanent unlock.** An always-on camera
driven by an autonomous process records the people around you, and those
people did not consent and cannot opt out. That is a different thing from a
tool you use, and it stays a decision a person makes.

`SELF_MODIFY` is deliberately *not* implemented here. Letting a system rewrite
its own enforcement code is the one grant that cannot be meaningfully
constrained by the code being constrained. The safe path is proposals plus
review, which is what `agent.py propose-change` is for.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Self


class Capability(StrEnum):
    """Durable capabilities, beyond the in-process ones in tools."""

    NETWORK = "NETWORK"
    CAMERA = "CAMERA"
    MICROPHONE = "MICROPHONE"
    VOICE_OUTPUT = "VOICE_OUTPUT"
    NOTIFICATIONS = "NOTIFICATIONS"
    SCHEDULER = "SCHEDULER"
    SEND_EMAIL = "SEND_EMAIL"
    POST_PUBLIC = "POST_PUBLIC"
    MOVE_MONEY = "MOVE_MONEY"

    @property
    def needs_human(self) -> bool:
        """Capabilities that affect people other than the operator.

        These are never self-grantable and are always surfaced in the inbox,
        because granting one is a decision with consequences outside this
        machine.
        """
        return self in {
            Capability.CAMERA,
            Capability.MICROPHONE,
            Capability.SEND_EMAIL,
            Capability.POST_PUBLIC,
            Capability.MOVE_MONEY,
        }

    @property
    def description(self) -> str:
        return {
            Capability.NETWORK: "fetch pages and call APIs",
            Capability.CAMERA: "capture images from a camera",
            Capability.MICROPHONE: "capture audio from a microphone",
            Capability.VOICE_OUTPUT: "speak through the speakers",
            Capability.NOTIFICATIONS: "raise desktop notifications",
            Capability.SCHEDULER: "run itself on a timer",
            Capability.SEND_EMAIL: "send email on your behalf",
            Capability.POST_PUBLIC: "publish content publicly",
            Capability.MOVE_MONEY: "transfer money between accounts",
        }[self]


class DenyReason(StrEnum):
    NOT_GRANTED = "not_granted"
    EXPIRED = "expired"
    OUT_OF_SCOPE = "out_of_scope"
    REVOKED = "revoked"


SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS security_grants (
        grant_id TEXT PRIMARY KEY,
        capability TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT '',
        granted_at TEXT NOT NULL,
        expires_at TEXT,
        note TEXT NOT NULL DEFAULT '',
        granted_by TEXT NOT NULL DEFAULT 'operator',
        state TEXT NOT NULL DEFAULT 'active'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS security_denials (
        denial_id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        capability TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL,
        context TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_grants_capability
    ON security_grants (capability, state)
    """,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Grant:
    """A permission that was actually given, with its limits."""

    capability: Capability
    scope: str = ""
    granted_at: str = field(default_factory=_now)
    expires_at: str | None = None
    note: str = ""
    granted_by: str = "operator"
    grant_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = "active"

    @property
    def expired(self) -> bool:
        deadline = _parse(self.expires_at or "")
        return deadline is not None and deadline <= datetime.now(UTC)

    @property
    def unlimited(self) -> bool:
        return not self.expires_at

    def covers(self, scope: str) -> bool:
        """Is `scope` inside this grant?

        An empty grant scope means the capability is granted outright. A
        scoped grant matches the scope itself or anything beneath it, so
        `data` covers `data/product/LISTING.md`.
        """
        if not self.scope:
            return True
        if not scope:
            return False
        if scope == self.scope:
            return True
        # A tuple of prefixes, in one call. Note that `startswith`'s second
        # positional argument is `start`, not another prefix, so passing two
        # separate prefixes would silently become a slice.
        prefix = self.scope.rstrip("/\\")
        return scope.startswith((f"{prefix}/", f"{prefix}\\"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "capability": str(self.capability),
            "description": self.capability.description,
            "scope": self.scope,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "unlimited": self.unlimited,
            "expired": self.expired,
            "note": self.note,
            "granted_by": self.granted_by,
            "state": self.state,
            "needs_human": self.capability.needs_human,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """The result of asking for a capability."""

    allowed: bool
    capability: Capability
    reason: DenyReason | str
    grant: Grant | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "capability": str(self.capability),
            "reason": str(self.reason),
            "detail": self.detail,
            "grant": self.grant.to_dict() if self.grant else None,
        }


class GrantStore:
    """Durable, revocable, auditable capability grants.

    Nothing is granted when this is constructed. That is the point: a
    capability that must be asked for cannot be exercised by accident.
    """

    def __init__(
        self,
        database_path: str | Path = "data/security.db",
        *,
        timeout: float = 5.0,
    ) -> None:
        memory = str(database_path) == ":memory:"
        self.database_path = Path(":memory:") if memory else Path(database_path)
        if not memory:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection: sqlite3.Connection | None = sqlite3.connect(
                str(self.database_path), timeout=timeout, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise RuntimeError(f"unable to open grant store: {exc}") from exc
        self._connection.row_factory = sqlite3.Row
        try:
            self.initialize()
        except Exception:
            self.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("grant store is closed")
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

    # -- granting ---------------------------------------------------------

    def grant(
        self,
        capability: Capability | str,
        *,
        scope: str = "",
        hours: float | None = None,
        note: str = "",
        granted_by: str = "operator",
    ) -> Grant:
        expires = (
            (datetime.now(UTC) + timedelta(hours=hours)).isoformat() if hours else None
        )
        record = Grant(
            capability=Capability(capability),
            scope=scope,
            expires_at=expires,
            note=note,
            granted_by=granted_by,
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO security_grants
                    (grant_id, capability, scope, granted_at, expires_at,
                     note, granted_by, state)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    record.grant_id,
                    str(record.capability),
                    record.scope,
                    record.granted_at,
                    record.expires_at,
                    record.note,
                    record.granted_by,
                ),
            )
        return record

    def revoke(self, capability: Capability | str, *, scope: str = "") -> int:
        """Revoke now. Takes effect on the very next `check`.

        Deliberately not cached: a cache would make revocation advisory, and
        the entire value of this module is that it is not.
        """
        parsed = Capability(capability)
        query = (
            "UPDATE security_grants SET state = 'revoked' "
            "WHERE capability = ? AND state = 'active'"
        )
        parameters: list[Any] = [str(parsed)]
        if scope:
            query += " AND scope = ?"
            parameters.append(scope)
        with self.connection:
            cursor = self.connection.execute(query, parameters)
        return cursor.rowcount

    def revoke_all(self) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE security_grants SET state = 'revoked' WHERE state = 'active'"
            )
        return cursor.rowcount

    def revoke_expired(self) -> int:
        """Mark lapsed grants revoked. Optional housekeeping."""
        now = _now()
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE security_grants SET state = 'expired' "
                "WHERE state = 'active' AND expires_at IS NOT NULL "
                "AND expires_at <= ?",
                (now,),
            )
        return cursor.rowcount

    # -- checking ---------------------------------------------------------

    def check(
        self,
        capability: Capability | str,
        scope: str = "",
        *,
        context: Mapping[str, Any] | None = None,
        record_denials: bool = True,
    ) -> Decision:
        """May this capability be used, right now, for this scope?"""
        parsed = Capability(capability)
        rows = self.connection.execute(
            "SELECT * FROM security_grants WHERE capability = ? AND state = 'active'",
            (str(parsed),),
        ).fetchall()
        if not rows:
            return self._deny(
                parsed,
                scope,
                DenyReason.NOT_GRANTED,
                f"{parsed} has not been granted",
                context,
                record_denials,
            )

        best: Grant | None = None
        saw_expired = False
        saw_out_of_scope = False
        for row in rows:
            candidate = self._row_to_grant(row)
            if candidate.expired:
                saw_expired = True
                continue
            if not candidate.covers(scope):
                saw_out_of_scope = True
                continue
            # Prefer the tightest matching scope: a narrow grant beats a
            # blanket one, so a later specific grant is not diluted.
            if best is None or len(candidate.scope) > len(best.scope):
                best = candidate

        if best is not None:
            return Decision(True, parsed, "granted", best)
        if saw_expired:
            return self._deny(
                parsed,
                scope,
                DenyReason.EXPIRED,
                f"the grant for {parsed} has expired",
                context,
                record_denials,
            )
        if saw_out_of_scope:
            return self._deny(
                parsed,
                scope,
                DenyReason.OUT_OF_SCOPE,
                f"{parsed} is granted, but not for {scope!r}",
                context,
                record_denials,
            )
        return self._deny(
            parsed,
            scope,
            DenyReason.NOT_GRANTED,
            "no usable grant",
            context,
            record_denials,
        )

    def _deny(
        self,
        capability: Capability,
        scope: str,
        reason: DenyReason,
        detail: str,
        context: Mapping[str, Any] | None,
        record: bool,
    ) -> Decision:
        if record:
            try:
                with self.connection:
                    self.connection.execute(
                        "INSERT INTO security_denials (at, capability, scope, "
                        "reason, context) VALUES (?, ?, ?, ?, ?)",
                        (
                            _now(),
                            str(capability),
                            scope,
                            str(reason),
                            json.dumps(
                                dict(context or {}), default=str, sort_keys=True
                            ),
                        ),
                    )
            except sqlite3.Error:
                pass
        return Decision(False, capability, reason, None, detail)

    # -- reading ----------------------------------------------------------

    @staticmethod
    def _row_to_grant(row: Mapping[str, Any]) -> Grant:
        return Grant(
            grant_id=row["grant_id"],
            capability=Capability(row["capability"]),
            scope=row["scope"] or "",
            granted_at=row["granted_at"],
            expires_at=row["expires_at"],
            note=row["note"] or "",
            granted_by=row["granted_by"] or "operator",
            state=row["state"],
        )

    def active(self) -> list[Grant]:
        rows = self.connection.execute(
            "SELECT * FROM security_grants WHERE state = 'active' "
            "ORDER BY capability, granted_at"
        ).fetchall()
        return [self._row_to_grant(row) for row in rows]

    def history(self, limit: int = 100) -> list[Grant]:
        rows = self.connection.execute(
            "SELECT * FROM security_grants ORDER BY granted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_grant(row) for row in rows]

    def denials(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM security_denials ORDER BY denial_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def granted(self) -> frozenset[Capability]:
        """Unlimited, unexpired, unscoped capabilities.

        Returned as a plain set for the in-process `PermissionPolicy`, which
        takes capabilities rather than grants.
        """
        return frozenset(
            grant.capability
            for grant in self.active()
            if not grant.expired and not grant.scope
        )

    def report(self) -> dict[str, Any]:
        active = self.active()
        return {
            "at": _now(),
            "granted": [grant.to_dict() for grant in active],
            "ungranted": [
                capability.value
                for capability in Capability
                if capability not in {grant.capability for grant in active}
            ],
            "needs_human": [
                grant.capability.value
                for grant in active
                if grant.capability.needs_human
            ],
            "recent_denials": self.denials(10),
        }
