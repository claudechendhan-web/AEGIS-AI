"""Escalation: the agent files a request when only a human can unblock it.

The agent is autonomous, and autonomy has one structural limit: there are
steps it cannot take no matter how well it is designed, because they require
an identity, a credential, a judgement, or a bank account. Opening a
marketplace account, deciding whether a licence is safe to sell under, and
moving money are not tasks that can be automated, and pretending otherwise
would be dishonest.

Rather than let the agent stall silently or pretend those steps are done, it
files a **request**. That turns an invisible wall into a queue you can read
and clear. The distinction matters: an agent that quietly gives up looks
identical to an agent that is working.

    python agent.py inbox                 # what is waiting on you
    python agent.py resolve 7 --note "listed, $36.52 tier, 3 downloads"

Each request declares a `kind`, because the right response differs:

    account     a venue must be created; you alone can do this
    credential  a secret is required and the agent must not hold it
    approval    a decision is needed, e.g. publish this, price it here
    payment     money has to move between accounts
    legal       a judgement with consequences you accept
    resource    hardware, disk, or a model that must be fetched
    contact     a human conversation with another person
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

OPEN = "open"
ANSWERED = "answered"
DISMISSED = "dismissed"
EXPIRED = "expired"

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS escalation_requests (
        request_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        severity TEXT NOT NULL,
        title TEXT NOT NULL,
        detail TEXT NOT NULL,
        unblock TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        resolution TEXT NOT NULL DEFAULT '',
        context TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS escalation_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        request_id TEXT NOT NULL,
        message TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_requests_state
    ON escalation_requests (state, severity, created_at)
    """,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Kind(StrEnum):
    ACCOUNT = "account"
    CREDENTIAL = "credential"
    APPROVAL = "approval"
    PAYMENT = "payment"
    LEGAL = "legal"
    RESOURCE = "resource"
    CONTACT = "contact"

    @property
    def label(self) -> str:
        return {
            Kind.ACCOUNT: "account or venue must be created",
            Kind.CREDENTIAL: "a secret is required",
            Kind.APPROVAL: "a decision is needed",
            Kind.PAYMENT: "money has to move",
            Kind.LEGAL: "a judgement with consequences",
            Kind.RESOURCE: "hardware or a model must be fetched",
            Kind.CONTACT: "a human conversation is required",
        }[self]


class Severity(StrEnum):
    BLOCKING = "blocking"
    DEGRADED = "degraded"
    FYI = "fyi"


@dataclass(frozen=True, slots=True)
class Request:
    """One thing the agent cannot do alone."""

    kind: Kind
    title: str
    detail: str
    unblock: str
    severity: Severity = Severity.BLOCKING
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    state: str = OPEN
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    resolution: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "kind": str(self.kind),
            "kind_label": self.kind.label,
            "severity": str(self.severity),
            "title": self.title,
            "detail": self.detail,
            "unblock": self.unblock,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolution": self.resolution,
            "context": dict(self.context),
        }


class RequestQueue:
    """Durable queue of blockers awaiting a human."""

    def __init__(
        self,
        database_path: str | Path = "data/escalations.db",
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
            raise RuntimeError(f"unable to open escalation queue: {exc}") from exc
        self._connection.row_factory = sqlite3.Row
        try:
            self.initialize()
        except Exception:
            self.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("escalation queue is closed")
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

    # -- writing ----------------------------------------------------------

    def file(self, request: Request) -> Request:
        """Add a request, unless an identical open one already exists.

        The dedup matters: an unattended run that re-detects the same blocker
        every round must not produce forty copies of one request.
        """
        existing = self.connection.execute(
            "SELECT * FROM escalation_requests WHERE state = ? AND kind = ? "
            "AND title = ?",
            (OPEN, str(request.kind), request.title),
        ).fetchone()
        if existing is not None:
            self._event(
                existing["request_id"],
                str(request.kind),
                "still blocked; request not duplicated",
            )
            return self._row_to_request(existing)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO escalation_requests
                    (request_id, kind, severity, title, detail, unblock, state,
                     created_at, updated_at, resolution, context)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
                """,
                (
                    request.request_id,
                    str(request.kind),
                    str(request.severity),
                    request.title,
                    request.detail,
                    request.unblock,
                    request.state,
                    request.created_at,
                    request.updated_at,
                    json.dumps(dict(request.context), default=str, sort_keys=True),
                ),
            )
        self._event(request.request_id, str(request.kind), request.title)
        return request

    def resolve(self, request_id: str, note: str = "") -> Request | None:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE escalation_requests SET state = ?, resolution = ?, "
                "updated_at = ? WHERE request_id = ? AND state = ?",
                (ANSWERED, note, _now(), request_id, OPEN),
            )
        if cursor.rowcount:
            self._event(request_id, "answer", note or "resolved")
            return self.get(request_id)
        return self.get(request_id)

    def dismiss(self, request_id: str, note: str = "") -> Request | None:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE escalation_requests SET state = ?, resolution = ?, "
                "updated_at = ? WHERE request_id = ? AND state = ?",
                (DISMISSED, note, _now(), request_id, OPEN),
            )
        if cursor.rowcount:
            self._event(request_id, "dismiss", note or "dismissed")
            return self.get(request_id)
        return self.get(request_id)

    def _event(self, request_id: str, kind: str, message: str) -> None:
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO escalation_events (at, kind, request_id, message) "
                    "VALUES (?, ?, ?, ?)",
                    (_now(), kind, request_id, message[:500]),
                )
        except sqlite3.Error:
            pass

    # -- reading ----------------------------------------------------------

    @staticmethod
    def _row_to_request(row: Mapping[str, Any]) -> Request:
        try:
            context = json.loads(row["context"] or "{}")
        except json.JSONDecodeError:
            context = {}
        return Request(
            request_id=row["request_id"],
            kind=Kind(row["kind"]),
            title=row["title"],
            detail=row["detail"],
            unblock=row["unblock"],
            severity=Severity(row["severity"]),
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolution=row["resolution"],
            context=context if isinstance(context, dict) else {},
        )

    def get(self, request_id: str) -> Request | None:
        row = self.connection.execute(
            "SELECT * FROM escalation_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        return self._row_to_request(row) if row else None

    def open_requests(self) -> list[Request]:
        order = {Severity.BLOCKING: 0, Severity.DEGRADED: 1, Severity.FYI: 2}
        rows = self.connection.execute(
            "SELECT * FROM escalation_requests WHERE state = ? ORDER BY created_at",
            (OPEN,),
        ).fetchall()
        requests = [self._row_to_request(row) for row in rows]
        return sorted(
            requests, key=lambda item: (order[item.severity], item.created_at)
        )

    def all_requests(self, limit: int = 100) -> list[Request]:
        rows = self.connection.execute(
            "SELECT * FROM escalation_requests ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_request(row) for row in rows]

    def events(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM escalation_events ORDER BY event_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT state, COUNT(*) AS n FROM escalation_requests GROUP BY state"
        ).fetchall()
        return {row["state"]: int(row["n"]) for row in rows}

    def __len__(self) -> int:
        return len(self.open_requests())


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------


def detect_blockers(
    *,
    model_available: bool,
    model_detail: str,
    spendable: Any,
    reserve: Any,
    approved_pending: int,
    blocked_categories: Mapping[str, str],
    published: bool = False,
    licence_confirmed: bool = False,
    package_records: int = 0,
    verified_records: int = 0,
) -> list[Request]:
    """Inspect the agent's own state and report what only a human can clear.

    The ordering is by how completely each item stops work, because that is
    the order you want to read them in.
    """
    found: list[Request] = []

    if not model_available:
        found.append(
            Request(
                kind=Kind.RESOURCE,
                severity=Severity.DEGRADED,
                title="No language model available",
                detail=(
                    f"{model_detail} The reasoning loop is offline. "
                    "Harvesting, retrieval, pricing, and the learning loop all "
                    "still work, so the agent is degraded rather than stopped."
                ),
                unblock="ollama pull qwen2.5-coder:3b",
                context={"model_detail": model_detail},
            )
        )

    if approved_pending and not published:
        found.append(
            Request(
                kind=Kind.PAYMENT,
                severity=Severity.BLOCKING,
                # Stable title, no count. The queue de-duplicates on
                # kind + title, so a title containing a running count never
                # matches and every round files a fresh copy, which is
                # precisely the spam the dedup exists to prevent.
                title="Approved work is waiting on a transaction",
                detail=(
                    f"{approved_pending} approved opportunit"
                    f"{'y is' if approved_pending == 1 else 'ies are'} funded "
                    "and waiting. The agent has work it is willing to pay for, "
                    "but it has no way to invoice, collect, or take payment. "
                    "Its spendable envelope cannot grow on its own, so this is "
                    "the point where it genuinely cannot continue without you."
                ),
                unblock=(
                    'Transact, then record it: python -c "from treasury '
                    "import Treasury, Money; t=Treasury('data/treasury.db'); "
                    "t.earn(Money.parse('<amount>','USD'), memo='sale')\" "
                    "-- then python agent.py resolve <id> --note 'done'"
                ),
                context={"approved_pending": approved_pending},
            )
        )

    if not published and package_records:
        found.append(
            Request(
                kind=Kind.ACCOUNT,
                severity=Severity.BLOCKING,
                title="Nothing is published, so nothing can sell",
                detail=(
                    f"{package_records:,} curated records are ready"
                    + (f" with {verified_records} verified" if verified_records else "")
                    + ". The listing is written to data/product/LISTING.md, but "
                    "publishing it requires a marketplace account, which "
                    "needs your identity and your payment details. The agent "
                    "cannot create one and will not impersonate you to do so."
                ),
                unblock=(
                    "Post data/product/LISTING.md on your marketplace, then "
                    "python agent.py resolve <id> --note 'listed at <url>'"
                ),
                context={
                    "records": package_records,
                    "verified": verified_records,
                    "listing": "data/product/LISTING.md",
                },
            )
        )

    if blocked_categories and len(blocked_categories) >= 3:
        found.append(
            Request(
                kind=Kind.APPROVAL,
                severity=Severity.DEGRADED,
                title=f"{len(blocked_categories)} categories are blocked",
                detail=(
                    "The track record has measured these as not paying, so the "
                    "agent has stopped proposing them. It has no new categories "
                    "left to try from its own closed vocabulary, so it needs "
                    "either new domains to probe or permission to lower the "
                    "hit-rate floor: "
                    + "; ".join(
                        f"{name} ({reason})"
                        for name, reason in sorted(blocked_categories.items())
                    )
                ),
                unblock=(
                    "python agent.py cycle --query '<new domain>' --opportunity "
                    "<category>:ask:cost:hours --help   # or lower the floor via "
                    "TrackRecord.set_policy"
                ),
                context={"blocked": dict(blocked_categories)},
            )
        )

    if not licence_confirmed and package_records:
        found.append(
            Request(
                kind=Kind.LEGAL,
                severity=Severity.BLOCKING,
                title="Upstream licence has not been cleared for resale",
                detail=(
                    "The corpus derives from a public dataset whose individual "
                    "records may carry third-party code licences. Selling a "
                    "derivative transfers those obligations to the buyer. The "
                    "agent will not make that call, and it will not accept an "
                    "instruction to skip the question."
                ),
                unblock=(
                    "Get a lawyer's view on the upstream licence, then "
                    "python agent.py resolve <id> --note 'cleared'"
                ),
                context={"records": package_records},
            )
        )

    # Sorted by how completely each item stops work, so any caller that just
    # iterates the result reads them in the order you would want to act.
    order = {Severity.BLOCKING: 0, Severity.DEGRADED: 1, Severity.FYI: 2}
    found.sort(key=lambda item: order[item.severity])
    return found


# --------------------------------------------------------------------------
# notification
# --------------------------------------------------------------------------


def notify(
    title: str, message: str, *, method: str = "auto", timeout: float = 10.0
) -> bool:
    """Best-effort desktop notification. Never raises, never blocks the agent.

    Notification is a courtesy, not a dependency. A machine with no toast
    support, or a headless session, simply gets nothing, and the queue on
    disk remains the source of truth.
    """
    if method == "none":
        return False
    short = message if len(message) <= 240 else message[:237] + "..."
    commands: list[list[str]] = []
    if method in ("auto", "msg"):
        commands.append(["cmd", "/c", "msg", "*", f"{title}: {short}"])
    if method in ("auto", "powershell"):
        escaped_title = title.replace("'", "''")
        escaped_body = short.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            f"$n.Icon=[System.Drawing.SystemIcons]::Information;"
            f"$n.BalloonTipTitle='{escaped_title}';"
            f"$n.BalloonTipText='{escaped_body}';"
            "$n.Visible=$true;$n.ShowBalloonTip(8000);Start-Sleep -s 9;"
            "$n.Dispose()"
        )
        commands.append(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ]
        )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
                shell=False,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def notify_new(queue: RequestQueue, *, method: str = "auto") -> int:
    """Notify about everything currently open. Returns how many were notified."""
    open_requests = queue.open_requests()
    if not open_requests:
        return 0
    blocking = [item for item in open_requests if item.severity == Severity.BLOCKING]
    chosen = blocking or open_requests
    sent = 0
    for request in chosen:
        if notify(
            f"AEGISAI [{request.kind}] {request.title}",
            f"{request.unblock}",
            method=method,
        ):
            sent += 1
    return sent
