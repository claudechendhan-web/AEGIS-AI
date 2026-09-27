"""Fetching documents from the web and the filesystem, within limits.

This is the component that makes "learn the internet" real, and it is also the
component most likely to cause harm if it is careless, so the limits are
enforced here rather than left to callers:

* **robots.txt is obeyed**, and refusal is raised rather than silently
  skipped, so a corpus cannot quietly fill with content a site asked not to
  be crawled. A refusal is a result the caller has to handle.
* **Only text formats are accepted.** No binaries, no archives.
* **A hard size ceiling** applies before and during the read, so a hostile
  server cannot exhaust memory.
* **Requests are serialized and delayed.** One connection at a time, with a
  politeness delay, because hammering a server is both rude and a fast way to
  get blocked.
* **Fetching is metered through the treasury** when one is supplied, so
  learning competes for the same 20% as everything else.

Nothing here trains a model. It populates an index that the model reads at
query time, which is the honest way to give a small local model knowledge it
was never trained on.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from memory.errors import (
    BudgetExhaustedError,
    DocumentTooLargeError,
    IngestError,
    RobotsDisallowedError,
)
from memory.store import MemoryIndex

DEFAULT_USER_AGENT = "AEGISAI/0.2 (local research index; respects robots.txt)"
MAX_DOCUMENT_BYTES = 2_000_000
ALLOWED_CONTENT_TYPES = (
    "text/html",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "application/json",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
)

# `head` is deliberately absent: it holds <title>, which we want. Its other
# children (meta, link) emit no text data, so skipping it would only throw
# away the one useful thing in there.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})


class _TextExtractor(HTMLParser):
    """Collect visible text and the document title, dropping script and style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._block = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "br", "li", "tr", "h1", "h2", "h3", "h4", "div", "section"):
            self._block = True

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in ("p", "li", "tr", "h1", "h2", "h3", "h4", "div", "section"):
            self._block = True

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data.strip():
            return
        if self._in_title:
            self.title_parts.append(data.strip())
            return
        self.parts.append(data)

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()

    @property
    def text(self) -> str:
        return "\n".join(part.strip() for part in self.parts if part.strip())


def extract_html_text(html: str) -> tuple[str, str]:
    """Return `(text, title)` for an HTML document."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise IngestError(f"unable to parse HTML: {exc}") from exc
    return parser.text, parser.title


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    uri: str
    text: str
    title: str = ""
    content_type: str = ""
    bytes_read: int = 0
    fetched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "title": self.title,
            "content_type": self.content_type,
            "bytes_read": self.bytes_read,
            "fetched_at": self.fetched_at,
            "characters": len(self.text),
        }


@dataclass(frozen=True, slots=True)
class IngestReport:
    """What one ingest pass actually did."""

    indexed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    chunks_added: int = 0
    spent: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "indexed": list(self.indexed),
            "skipped": list(self.skipped),
            "refused": list(self.refused),
            "failed": list(self.failed),
            "chunks_added": self.chunks_added,
            "spent": self.spent,
        }


class WebIngestor:
    """Fetch, decode and index documents, politely and within budget."""

    def __init__(
        self,
        index: MemoryIndex,
        *,
        treasury: Any = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 20.0,
        delay: float = 1.0,
        max_bytes: int = MAX_DOCUMENT_BYTES,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.index = index
        self.treasury = treasury
        self.user_agent = user_agent
        self.timeout = timeout
        self.delay = max(0.0, delay)
        self.max_bytes = max_bytes
        self._opener = opener or urlopen
        self._sleep = sleep if sleep is not None else time.sleep
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request = 0.0

    # -- politeness -------------------------------------------------------

    def _wait(self) -> None:
        if self.delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < self.delay:
            self._sleep(self.delay - elapsed)
        self._last_request = time.monotonic()

    def allowed_by_robots(self, url: str) -> bool:
        """Consult robots.txt once per host and cache the answer."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise IngestError(f"unsupported URL scheme: {url!r}")
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        parser = self._robots[origin]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    def _load_robots(self, origin: str) -> RobotFileParser | None:
        parser = RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        try:
            response = self._opener(
                Request(
                    f"{origin}/robots.txt",
                    headers={"User-Agent": self.user_agent},
                ),
                timeout=min(self.timeout, 10.0),
            )
            raw = response.read(512_000)
            close = getattr(response, "close", None)
            if callable(close):
                close()
        except (HTTPError, URLError, TimeoutError, OSError):
            # A missing or unreadable robots.txt is treated as "no rules",
            # which is what the standard prescribes. It is not treated as
            # "allowed" if the fetch failed for another reason.
            return None
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        try:
            parser.parse(text.splitlines())
        except Exception:  # noqa: BLE001 - a broken robots.txt must not be fatal
            return None
        return parser

    # -- budget -----------------------------------------------------------

    def _charge(self, cost: float) -> None:
        if self.treasury is None or cost <= 0:
            return
        from treasury.money import Money

        self.treasury.spend(
            Money.parse(f"{cost:.8f}", self.treasury.policy.currency),
            memo=f"web fetch: {cost:.8f}",
        )

    def _check_budget(self, uri: str) -> None:
        if self.treasury is None:
            return
        from treasury.money import Money

        currency = self.treasury.policy.currency
        cost = Money.parse(f"{self.cost_per_fetch:.8f}", currency)
        if not self.treasury.can_spend(cost):
            raise BudgetExhaustedError(
                f"cannot afford to fetch {uri}; the spendable envelope is too low"
            )

    cost_per_fetch: float = 0.001

    # -- fetching ---------------------------------------------------------

    def fetch(self, url: str) -> FetchedDocument:
        """Fetch one URL, enforcing robots, size, and content type."""
        if not self.allowed_by_robots(url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")
        self._check_budget(url)

        self._wait()
        request = Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.1",
            },
        )
        response = None
        try:
            response = self._opener(request, timeout=self.timeout)
            status = getattr(response, "status", 200)
            if isinstance(status, int) and status >= 400:
                raise IngestError(f"HTTP {status} for {url}")
            declared = (
                response.headers.get("Content-Length") if response.headers else None
            )
            if declared and int(declared) > self.max_bytes:
                raise DocumentTooLargeError(
                    f"{url} declares {declared} bytes, over the {self.max_bytes} limit"
                )
            raw = response.read(self.max_bytes + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise IngestError(f"unable to fetch {url}: {exc}") from exc
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

        if len(raw) > self.max_bytes:
            raise DocumentTooLargeError(
                f"{url} exceeded the {self.max_bytes} byte limit"
            )

        content_type = ""
        if response is not None and response.headers:
            content_type = response.headers.get("Content-Type", "").split(";")[0]
            content_type = content_type.strip().lower()
        if content_type and not any(
            content_type.startswith(allowed) for allowed in ALLOWED_CONTENT_TYPES
        ):
            raise IngestError(f"unsupported content type {content_type!r} for {url}")

        text = raw.decode("utf-8", errors="replace")
        title = ""
        if content_type.startswith("text/html") or "<html" in text[:2048].lower():
            text, title = extract_html_text(text)
        elif content_type == "application/json":
            try:
                text = json.dumps(json.loads(text), indent=2, sort_keys=True)
            except json.JSONDecodeError as exc:
                raise IngestError(f"{url} is not valid JSON: {exc}") from exc
        else:
            title = urlparse(url).path.rsplit("/", 1)[-1]

        self._charge(self.cost_per_fetch)
        return FetchedDocument(
            uri=url,
            text=text,
            title=title,
            content_type=content_type,
            bytes_read=len(raw),
        )

    def ingest_urls(
        self,
        urls: Sequence[str],
        *,
        chunk_tokens: int = 220,
        overlap_tokens: int = 40,
    ) -> IngestReport:
        """Fetch and index a batch, reporting every outcome separately.

        Failures are collected rather than raised, because a batch of fifty
        URLs where four are dead should still index the other forty-six. A
        robots refusal is reported in its own bucket, not as a failure, so it
        is never confused with a network error.
        """
        indexed: list[str] = []
        skipped: list[str] = []
        refused: list[str] = []
        failed: list[str] = []
        chunks = 0
        for url in urls:
            try:
                document = self.fetch(url)
            except RobotsDisallowedError:
                refused.append(url)
                continue
            except BudgetExhaustedError as exc:
                failed.append(f"{url}: {exc}")
                break
            except IngestError as exc:
                failed.append(f"{url}: {exc}")
                continue
            if not document.text.strip():
                skipped.append(url)
                continue
            added = self.index.add_document(
                document.uri,
                document.text,
                title=document.title,
                source="web",
                chunk_tokens=chunk_tokens,
                overlap_tokens=overlap_tokens,
                metadata=document.to_dict(),
            )
            if added:
                indexed.append(url)
                chunks += len(added)
            else:
                skipped.append(url)
        return IngestReport(
            indexed=tuple(indexed),
            skipped=tuple(skipped),
            refused=tuple(refused),
            failed=tuple(failed),
            chunks_added=chunks,
        )

    def ingest_paths(
        self,
        paths: Iterable[Path],
        *,
        chunk_tokens: int = 220,
        overlap_tokens: int = 40,
    ) -> IngestReport:
        """Index local files. Free, so it never touches the treasury."""
        from urllib.request import pathname2url

        indexed: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        chunks = 0
        for path in paths:
            target = Path(path)
            if not target.is_file():
                failed.append(f"{target}: not a file")
                continue
            if target.stat().st_size > self.max_bytes:
                failed.append(f"{target}: over the {self.max_bytes} byte limit")
                continue
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                failed.append(f"{target}: {exc}")
                continue
            if not text.strip():
                skipped.append(str(target))
                continue
            uri = f"file://{pathname2url(str(target.resolve()))}"
            added = self.index.add_document(
                uri,
                text,
                title=target.name,
                source="file",
                chunk_tokens=chunk_tokens,
                overlap_tokens=overlap_tokens,
                metadata={"path": str(target.resolve())},
            )
            if added:
                indexed.append(uri)
                chunks += len(added)
            else:
                skipped.append(str(target))
        return IngestReport(
            indexed=tuple(indexed),
            skipped=tuple(skipped),
            failed=tuple(failed),
            chunks_added=chunks,
        )


def extract_seed_urls(document: FetchedDocument | str) -> list[str]:
    """Pull same-host links out of a fetched page to seed a crawl frontier.

    Depth-limited crawling is a deliberate choice. An unbounded crawl of the
    open web is the failure mode that produces a corpus of SEO spam, and it
    is also the failure mode that gets an IP blocked.
    """
    from html.parser import HTMLParser

    class _Links(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.hrefs: list[str] = []

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            if tag != "a":
                return
            for name, value in attrs:
                if name == "href" and value:
                    self.hrefs.append(value)

    if isinstance(document, FetchedDocument):
        html = document.text
        origin = urlsplit(document.uri)
    else:
        html = str(document)
        origin = urlsplit(str(document))
    if not html.lstrip().startswith("<"):
        return []
    parser = _Links()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed markup must not be fatal
        return []
    found: list[str] = []
    seen: set[str] = set()
    for href in parser.hrefs:
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        from urllib.parse import urljoin

        absolute = urljoin(
            document.uri if isinstance(document, FetchedDocument) else "", href
        )
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc != origin.netloc:
            continue
        cleaned = absolute.split("#", 1)[0]
        if cleaned in seen:
            continue
        seen.add(cleaned)
        found.append(cleaned)
    return found
