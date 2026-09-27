"""The overnight harvest worker.

This is the closest honest thing to "train it on the internet for 24 hours".
It does not train anything and it will never, because this machine cannot.
What it does is build a real, durable knowledge base: it crawls a configured
set of documentation sources into the local BM25 index, so that tomorrow the
agent can answer questions about the material it has actually read instead of
only what a 7B model happened to memorize.

Design constraints, all of them forced by the fact that nobody is watching:

* **A hard wall-clock deadline.** The job stops when the budget runs out, and
  that is a success, not an error. An unbounded crawler started before you go
  to sleep is a process you cannot reason about in the morning.
* **Resumable.** Queue, failures, and per-host backoff all live in SQLite, so
  a crash, a reboot, or a deliberate stop loses nothing.
* **Every request is metered** through the treasury and bounded by the
  spendable envelope. It stops cleanly when it cannot afford the next fetch.
  The reserve is never touched.
* **robots.txt is obeyed** and refusals are counted, not hidden.
* **Politeness is not optional.** Serial requests, a configurable delay, and a
  per-host backoff after repeated failure. A crawler that hammers a host gets
  blocked, and then it has learned nothing and annoyed someone.
* **Depth and page caps**, so a documentation site cannot quietly consume the
  entire night.
* **A status file** is rewritten every cycle, so progress can be read without
  attaching to the process.

Run it detached, then check on it:

    python -m harvest start --deadline-hours 8
    python -m harvest status
    python -m harvest stop
"""

from __future__ import annotations

import json
import signal
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

from harvest.errors import DeadlineReachedError, SourceConfigError
from harvest.frontier import DONE, FAILED, SKIPPED, Frontier, FrontierItem
from memory.errors import (
    BudgetExhaustedError,
    IngestError,
    RobotsDisallowedError,
)
from memory.ingest import WebIngestor, extract_seed_urls
from memory.store import MemoryIndex
from treasury.money import Money
from treasury.treasury import Treasury

DEFAULT_STATUS_PATH = Path("data/harvest_status.json")
DEFAULT_INDEX_PATH = Path("data/memory.db")
DEFAULT_FRONTIER_PATH = Path("data/harvest.db")
DEFAULT_TREASURY_PATH = Path("data/treasury.db")

DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_PAGES = 2000
DEFAULT_CYCLE_PAGES = 25

# Conservative, documentation-shaped starting points. Every one of these is
# public technical documentation with a permissive licence and a robots.txt
# that permits crawling. Add sources deliberately rather than by pattern.
DEFAULT_SOURCES: tuple[str, ...] = (
    "https://docs.python.org/3/library/stdtypes.html",
    "https://docs.python.org/3/library/sqlite3.html",
    "https://docs.python.org/3/library/decimal.html",
    "https://docs.python.org/3/library/argparse.html",
    "https://docs.python.org/3/library/pathlib.html",
    "https://docs.python.org/3/library/dataclasses.html",
    "https://docs.python.org/3/library/urllib.request.html",
    "https://docs.python.org/3/library/collections.html",
    "https://docs.python.org/3/library/itertools.html",
    "https://docs.python.org/3/library/json.html",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class HarvestStats:
    """Counters for the current run."""

    started_at: str = field(default_factory=_now)
    cycles: int = 0
    attempted: int = 0
    indexed: int = 0
    chunks: int = 0
    refused: int = 0
    failed: int = 0
    skipped: int = 0
    spent: str = "0.00000000"
    stopped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "cycles": self.cycles,
            "attempted": self.attempted,
            "indexed": self.indexed,
            "chunks": self.chunks,
            "refused": self.refused,
            "failed": self.failed,
            "skipped": self.skipped,
            "spent": self.spent,
            "stopped_reason": self.stopped_reason,
        }


@dataclass(slots=True)
class HarvestConfig:
    """Everything the worker needs, in one auditable place."""

    sources: tuple[str, ...] = DEFAULT_SOURCES
    deadline_hours: float = 8.0
    max_depth: int = DEFAULT_MAX_DEPTH
    max_pages: int = DEFAULT_MAX_PAGES
    cycle_pages: int = DEFAULT_CYCLE_PAGES
    delay_seconds: float = DEFAULT_DELAY_SECONDS
    chunk_tokens: int = 320
    overlap_tokens: int = 48
    follow_links: bool = True
    index_path: Path = DEFAULT_INDEX_PATH
    frontier_path: Path = DEFAULT_FRONTIER_PATH
    treasury_path: Path = DEFAULT_TREASURY_PATH
    status_path: Path = DEFAULT_STATUS_PATH
    cost_per_fetch: float = 0.001
    settle_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.deadline_hours <= 0:
            raise SourceConfigError("deadline_hours must be positive")
        if self.max_depth < 0:
            raise SourceConfigError("max_depth must not be negative")
        if self.max_pages <= 0:
            raise SourceConfigError("max_pages must be positive")
        if self.cycle_pages <= 0:
            raise SourceConfigError("cycle_pages must be positive")
        if self.delay_seconds < 0:
            raise SourceConfigError("delay_seconds must not be negative")
        cleaned = tuple(source.strip() for source in self.sources if source.strip())
        if not cleaned:
            raise SourceConfigError("at least one source url is required")
        object.__setattr__(self, "sources", cleaned)


class Deadline:
    """Monotonic wall-clock budget with a cooperative stop flag."""

    def __init__(self, hours: float, settle_seconds: float = 30.0) -> None:
        self.deadline = time.monotonic() + hours * 3600.0
        self.settle_seconds = max(0.0, settle_seconds)
        self._stop = False

    def install_signal_handlers(self) -> None:
        def handler(signum: int, frame: Any) -> None:
            self._stop = True

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    # Not on the main thread, or unsupported on this platform.
                    pass

    @property
    def stopping(self) -> bool:
        return self._stop

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def expired(self, *, reserve: float = 0.0) -> bool:
        return self.remaining() <= reserve


class HarvestWorker:
    """Bounded, resumable, treasury-metered documentation harvester."""

    def __init__(
        self,
        config: HarvestConfig | None = None,
        *,
        index: MemoryIndex | None = None,
        frontier: Frontier | None = None,
        treasury: Treasury | None = None,
        ingestor_factory: Callable[[], WebIngestor] | None = None,
        sleep: Any = None,
        monotonic: Any = None,
    ) -> None:
        self.config = config or HarvestConfig()
        self.index = index or MemoryIndex(self.config.index_path)
        self.frontier = frontier or Frontier(self.config.frontier_path)
        self.treasury = treasury
        self.ingestor_factory = ingestor_factory
        self.stats = HarvestStats()
        self._sleep = sleep if sleep is not None else time.sleep
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._owns_resources = index is None and frontier is None

    def close(self) -> None:
        if self._owns_resources:
            self.frontier.close()
            self.index.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- lifecycle --------------------------------------------------------

    def seed(self) -> int:
        """Enqueue the configured sources, if not already present."""
        added = self.frontier.add(self.config.sources, depth=0)
        self.frontier.log("seed", detail=f"{added} source url(s)")
        return added

    def run(self) -> HarvestStats:
        """Harvest until the deadline, the page cap, or the frontier empties."""
        deadline = Deadline(self.config.deadline_hours, self.config.settle_seconds)
        deadline.install_signal_handlers()
        self.seed()
        ingestor = self._build_ingestor()

        try:
            while True:
                if deadline.stopping:
                    self._stop("signal")
                    break
                if deadline.expired():
                    self._stop("deadline")
                    break
                if self.stats.attempted >= self.config.max_pages:
                    self._stop("max_pages")
                    break
                if self.frontier.pending_estimate() == 0:
                    # Do not overwrite a specific reason such as "budget".
                    # Running out of money and running out of work are very
                    # different mornings, and the specific one is the useful
                    # one.
                    self._stop("frontier_empty")
                    break

                budget = min(
                    self.config.cycle_pages,
                    self.config.max_pages - self.stats.attempted,
                )
                processed = self._cycle(ingestor, deadline, budget)
                self.stats.cycles += 1
                self._write_status()
                if self.stats.stopped_reason == "budget":
                    break
                if processed == 0 and deadline.expired(
                    reserve=self.config.settle_seconds
                ):
                    self._stop("deadline")
                    break
                if processed:
                    self._sleep(self.config.settle_seconds)
        except DeadlineReachedError:
            self._stop("deadline")
        except KeyboardInterrupt:
            self._stop("interrupt")
        finally:
            self._write_status()
            self.frontier.log(
                "stop", detail=json.dumps(self.stats.to_dict(), sort_keys=True)
            )
        return self.stats

    def _stop(self, reason: str) -> None:
        """Record why the run ended, keeping the most specific reason."""
        if not self.stats.stopped_reason:
            self.stats.stopped_reason = reason

    def _build_ingestor(self) -> WebIngestor:
        if self.ingestor_factory is not None:
            return self.ingestor_factory()
        ingestor = WebIngestor(
            self.index,
            treasury=self.treasury,
            delay=self.config.delay_seconds,
            sleep=self._sleep,
        )
        ingestor.cost_per_fetch = self.config.cost_per_fetch
        return ingestor

    # -- one cycle --------------------------------------------------------

    def _cycle(self, ingestor: WebIngestor, deadline: Deadline, budget: int) -> int:
        items = self.frontier.take(limit=budget)
        if not items:
            return 0
        processed = 0
        for item in items:
            if deadline.stopping or deadline.expired(
                reserve=self.config.settle_seconds
            ):
                # Put the untouched remainder back so the next run resumes
                # here instead of losing the tail of the queue.
                self.frontier.restore(entry.url for entry in items[processed:])
                break
            self._process(ingestor, item)
            processed += 1
        return processed

    def _process(self, ingestor: WebIngestor, item: FrontierItem) -> None:
        self.stats.attempted += 1
        before = self._operate_balance()
        try:
            document = ingestor.fetch(item.url)
        except RobotsDisallowedError:
            self.stats.refused += 1
            self.frontier.mark(item.url, SKIPPED, "robots.txt disallows this path")
            self.frontier.log("refused", item.url, "robots.txt")
            return
        except BudgetExhaustedError as exc:
            self._stop("budget")
            self.frontier.mark(item.url, SKIPPED, str(exc))
            self.frontier.penalize_host(item.host)
            self.frontier.log("budget", item.url, str(exc))
            return
        except IngestError as exc:
            self.stats.failed += 1
            self.frontier.mark(item.url, FAILED, str(exc))
            if item.attempts >= self.frontier.max_attempts:
                self.frontier.penalize_host(item.host)
            self.frontier.log("failed", item.url, str(exc))
            return

        if not document.text.strip():
            self.stats.skipped += 1
            self.frontier.mark(item.url, SKIPPED, "no extractable text")
            return

        chunks = self.index.add_document(
            document.uri,
            document.text,
            title=document.title or item.url,
            source="web",
            chunk_tokens=self.config.chunk_tokens,
            overlap_tokens=self.config.overlap_tokens,
            metadata=document.to_dict(),
        )
        if chunks:
            self.stats.indexed += 1
            self.stats.chunks += len(chunks)
        self._record_spend(before)
        self.frontier.mark(item.url, DONE)
        self.frontier.log(
            "indexed",
            item.url,
            json.dumps(
                {"chunks": len(chunks), "title": document.title}, sort_keys=True
            ),
        )

        if self.config.follow_links and item.depth < self.config.max_depth:
            for link in extract_seed_urls(document)[:200]:
                self.frontier.add([link], depth=item.depth + 1)

    # -- treasury ---------------------------------------------------------

    def _operate_balance(self) -> Money:
        if self.treasury is None:
            return Money.zero("USD")
        return self.treasury.ledger.balance(self.treasury.policy.operate.account_id)

    def _record_spend(self, before: Money) -> None:
        """Add the cost of one fetch to the run total.

        The delta is `before - after`, not `after - before`. Spending reduces
        the envelope balance, so the naive difference is negative and a
        `> 0` guard on it discards every charge: the run then reports zero
        spent while the treasury is quietly paying for every request.
        """
        delta = before - self._operate_balance()
        if delta.amount <= 0:
            return
        total = Decimal(self.stats.spent) + delta.amount
        self.stats.spent = format(total, "f")

    # -- status -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "at": _now(),
            "running": True,
            "config": {
                "deadline_hours": self.config.deadline_hours,
                "max_depth": self.config.max_depth,
                "max_pages": self.config.max_pages,
                "delay_seconds": self.config.delay_seconds,
                "sources": list(self.config.sources),
            },
            "stats": self.stats.to_dict(),
            "frontier": self.frontier.counts(),
            "index": self.index.stats().to_dict(),
            "treasury": (self.treasury.status() if self.treasury is not None else None),
        }

    def _write_status(self) -> None:
        """Write the status file atomically.

        Written to a temporary file and moved into place, so a reader never
        observes a half-written JSON document. A status file that is truncated
        exactly when you go to look at it is worse than no status file.
        """
        path = self.config.status_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(self.status(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            pass


def load_status(path: str | Path = DEFAULT_STATUS_PATH) -> dict[str, Any] | None:
    """Read the status file, or None if the job has never run."""
    target = Path(path)
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def summarize_failures(frontier: Frontier, limit: int = 20) -> list[dict[str, Any]]:
    return frontier.iter_failures(limit)


def build_config(
    sources: Sequence[str] | None = None,
    **overrides: Any,
) -> HarvestConfig:
    """Build a config from CLI arguments, dropping unset values."""
    payload: dict[str, Any] = {}
    if sources:
        payload["sources"] = tuple(sources)
    for key, value in overrides.items():
        if value is not None:
            payload[key] = value
    return HarvestConfig(**payload)


def default_sources() -> tuple[str, ...]:
    return DEFAULT_SOURCES


def allowed_source_hosts(sources: Iterable[str]) -> tuple[str, ...]:
    from urllib.parse import urlsplit

    return tuple(
        dict.fromkeys(
            urlsplit(source.strip()).netloc
            for source in sources
            if urlsplit(source.strip()).netloc
        )
    )
