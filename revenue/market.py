"""Probe whether a market actually exists, before spending money on it.

This is the "try to make money" component, in the only form that is honest
without accounts, credentials, or a payment rail: go and find out whether
anyone is buying the thing, and what the competition looks like.

The probe reads HuggingFace's public datasets API. For each comparable
listing it records real engagement numbers:

    downloads   how many times it was fetched
    likes       how many people endorsed it
    updated     recency of maintenance, a liveness signal

What those numbers do and do not mean, because the difference matters:

* Downloads and likes are a **demand signal**, and a real one. Thousands of
  people fetching a dataset in a domain is evidence that models are being
  trained on that domain, which means someone downstream paid for training
  runs and needed the data.
* Downloads are **not revenue**. Almost everything on this platform is free.
  A dataset with 50,000 downloads may have earned nothing.
* So this probe answers "is anyone building models on this?" and therefore
  "is curation and verification of this worth selling?". It does **not**
  answer "will this sell?", and nothing here can.

That distinction is the whole reason this module refuses to return a verdict
of "profitable". It returns evidence and a demand read, and leaves the
commercial judgement to a human.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from revenue.opportunity import Evidence, Opportunity
from treasury.money import Money

HF_DATASETS_API = "https://huggingface.co/api/datasets"
DEFAULT_USER_AGENT = "AEGISAI/0.2 (local market probe)"

# Demand thresholds, chosen to be conservative and explainable rather than
# clever. They are thresholds, not scores.
VIABLE_DOWNLOAD_MEDIAN = Decimal(1000)
CROWDED_COMPETITORS = 25


class MarketProbeError(Exception):
    """Raised when a market probe cannot be performed."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Listing:
    """One comparable dataset found on the platform."""

    identifier: str
    downloads: int
    likes: int
    tags: tuple[str, ...] = ()
    updated: str = ""

    @property
    def engagement(self) -> int:
        return self.downloads + self.likes * 10

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "downloads": self.downloads,
            "likes": self.likes,
            "engagement": self.engagement,
            "updated": self.updated,
            "tags": list(self.tags[:6]),
        }


@dataclass(frozen=True, slots=True)
class DemandSignal:
    """One query's worth of evidence."""

    query: str
    listings: tuple[Listing, ...]
    fetched_at: str = field(default_factory=_now)

    @property
    def count(self) -> int:
        return len(self.listings)

    @property
    def total_downloads(self) -> int:
        return sum(item.downloads for item in self.listings)

    @property
    def median_downloads(self) -> Decimal:
        """Median downloads across the listings.

        Not quantized to a whole number. A median of 21 and 30 is 25.5, and
        rounding it to 26 reports a figure the data does not support.
        """
        if not self.listings:
            return Decimal(0)
        values = sorted(item.downloads for item in self.listings)
        middle = len(values) // 2
        if len(values) % 2:
            return Decimal(values[middle])
        return (Decimal(values[middle - 1] + values[middle]) / Decimal(2)).quantize(
            Decimal("0.01")
        )

    @property
    def top(self) -> Listing | None:
        if not self.listings:
            return None
        return max(self.listings, key=lambda item: item.engagement)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "listings_found": self.count,
            "total_downloads": self.total_downloads,
            "median_downloads": format(self.median_downloads, "f"),
            "best": self.top.to_dict() if self.top else None,
            "fetched_at": self.fetched_at,
        }


@dataclass(frozen=True, slots=True)
class MarketReport:
    """The evidence, and what it does and does not support."""

    signals: tuple[DemandSignal, ...]
    viable: bool
    crowded: bool
    reasons: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()

    @property
    def total_downloads(self) -> int:
        return sum(signal.total_downloads for signal in self.signals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "probed_at": _now(),
            "queries": [signal.to_dict() for signal in self.signals],
            "aggregate": {
                "listings_found": sum(signal.count for signal in self.signals),
                "total_downloads": self.total_downloads,
                "median_downloads_across_queries": format(
                    statistics.median(
                        [float(signal.median_downloads) for signal in self.signals]
                    ),
                    ".1f",
                )
                if self.signals
                else "0",
            },
            "demand_read": {
                "viable": self.viable,
                "crowded": self.crowded,
                "reasons": list(self.reasons),
            },
            "what_this_does_not_tell_you": list(self.caveats),
        }


DEFAULT_CAVEATS = (
    (
        "Downloads are not revenue. Most datasets on this platform are free, "
        "so a listing with many downloads may have earned nothing."
    ),
    (
        "This measures whether anyone builds models in the domain, not "
        "whether they will pay you for curation."
    ),
    "Engagement numbers are a proxy for attention, not for willingness to pay.",
    (
        "No price was observed anywhere in this probe, because the platform "
        "does not publish one."
    ),
)


class MarketProbe:
    """Query a public dataset marketplace and read real engagement."""

    def __init__(
        self,
        *,
        timeout: float = 25.0,
        user_agent: str = DEFAULT_USER_AGENT,
        opener: Any = None,
        limit: int = 50,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.limit = max(1, limit)
        self._opener = opener or urlopen

    def _get(self, url: str) -> Any:
        request = Request(url, headers={"User-Agent": self.user_agent})
        response = None
        try:
            response = self._opener(request, timeout=self.timeout)
            raw = response.read()
        except HTTPError as exc:
            raise MarketProbeError(f"HTTP {exc.code} from the marketplace") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MarketProbeError(f"unable to reach the marketplace: {exc}") from exc
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MarketProbeError("marketplace returned invalid JSON") from exc

    def search(self, query: str, *, limit: int | None = None) -> DemandSignal:
        count = max(1, min(limit or self.limit, 100))
        listings = self._query(query, count)

        # The marketplace's search is a literal term match over the whole
        # string, so a natural-language query like "scientific python" matches
        # nothing at all. Left alone that reads as "no demand here", which is
        # a much stronger claim than "we did not know how to ask" - and it
        # silently biases the gates toward refusing. Fall back to the
        # individual terms so a multi-word query still measures something.
        attempted = [query]
        if not listings and len(query.split()) > 1:
            merged: dict[str, Listing] = {}
            for term in query.split():
                attempted.append(term)
                for listing in self._query(term, count):
                    merged.setdefault(listing.identifier, listing)
            listings = sorted(
                merged.values(), key=lambda item: item.downloads, reverse=True
            )[:count]

        if not listings and len(attempted) > 1:
            # No signal at all, and we exhausted the term fallback. Return an
            # empty signal rather than a wrong one; `probe_market` already
            # attaches the standing caveats to the report.
            return DemandSignal(query=query, listings=())
        return DemandSignal(query=query, listings=tuple(listings))

    def _query(self, query: str, count: int) -> list[Listing]:
        url = (
            f"{HF_DATASETS_API}?search={quote_plus(query)}"
            f"&limit={count}&full=false&sort=downloads&direction=-1"
        )
        payload = self._get(url)
        if not isinstance(payload, list):
            return []
        listings: list[Listing] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            identifier = item.get("id")
            if not isinstance(identifier, str) or not identifier:
                continue
            tags = item.get("tags")
            listings.append(
                Listing(
                    identifier=identifier,
                    downloads=int(item.get("downloads") or 0),
                    likes=int(item.get("likes") or 0),
                    tags=tuple(str(tag) for tag in tags)
                    if isinstance(tags, list)
                    else (),
                    updated=str(item.get("lastModified") or ""),
                )
            )
        return listings


def probe_market(
    queries: Sequence[str],
    *,
    viable_median: int = int(VIABLE_DOWNLOAD_MEDIAN),
    crowded_at: int = CROWDED_COMPETITORS,
    probe: MarketProbe | None = None,
    caveats: Iterable[str] = DEFAULT_CAVEATS,
) -> MarketReport:
    """Probe every query and read the demand evidence."""
    if not queries:
        raise MarketProbeError("at least one query is required")
    engine = probe or MarketProbe()
    signals: list[DemandSignal] = []
    failures: list[str] = []
    for query in queries:
        try:
            signals.append(engine.search(query))
        except MarketProbeError as exc:
            failures.append(f"{query}: {exc}")
    if not signals:
        raise MarketProbeError(
            "every probe failed: " + "; ".join(failures) if failures else "no results"
        )

    total_listings = sum(signal.count for signal in signals)
    medians = [signal.median_downloads for signal in signals]
    best_median = max(medians) if medians else Decimal(0)
    viable = best_median >= Decimal(viable_median)
    crowded = total_listings >= crowded_at

    reasons: list[str] = []
    for signal in signals:
        reasons.append(
            f"{signal.query!r}: {signal.count} listing(s), "
            f"{signal.total_downloads} downloads, "
            f"median {format(signal.median_downloads, 'f')}"
        )
    if viable:
        reasons.append(
            f"median downloads of {format(best_median, 'f')} clear the "
            f"{viable_median} viability threshold"
        )
    else:
        reasons.append(
            f"best median downloads {format(best_median, 'f')} is below the "
            f"{viable_median} viability threshold: weak demand evidence"
        )
    if crowded:
        reasons.append(
            f"{total_listings} comparable listings is crowded (>= {crowded_at}); "
            "differentiated curation is required to compete"
        )
    else:
        reasons.append(
            f"only {total_listings} comparable listing(s): the space is open, "
            "which cuts both ways"
        )
    reasons.extend(failures)

    return MarketReport(
        signals=tuple(signals),
        viable=viable,
        crowded=crowded,
        reasons=tuple(reasons),
        caveats=tuple(caveats),
    )


def opportunity_from_market(
    report: MarketReport,
    *,
    ask_price: Money,
    build_cost: Money,
    effort_hours: float,
    evidence: Sequence[Mapping[str, Any]] = (),
    currency: str = "USD",
) -> Opportunity:
    """Turn a demand read into a costed, evidence-backed opportunity.

    Only sensible once `report.viable` is true. A market with no demand
    produces an opportunity that every gate will refuse anyway, and building
    one anyway invites the model to talk itself into a bad bet.
    """
    if not report.viable:
        raise MarketProbeError(
            "refusing to build an opportunity from a market with no demand "
            "evidence: " + "; ".join(report.reasons[-2:])
        )
    items: list[Evidence] = []
    for signal in report.signals:
        best = signal.top
        if best is None:
            continue
        items.append(
            Evidence(
                uri=f"https://huggingface.co/datasets/{best.identifier}",
                title=f"comparable dataset {best.identifier}",
                snippet=(
                    f"{best.downloads} downloads, {best.likes} likes; "
                    "observed demand for this domain"
                ),
            )
        )
    return Opportunity(
        category="data_product",
        summary=(
            f"Curated and execution-verified dataset for a domain with "
            f"{report.total_downloads} observed downloads across "
            f"{len(report.signals)} probe(s)."
        ),
        ask_price=ask_price,
        expected_cost=build_cost,
        effort_hours=effort_hours,
        evidence=tuple(items),
        metadata={
            "market_viable": report.viable,
            "market_crowded": report.crowded,
            "total_downloads": report.total_downloads,
        },
    )
