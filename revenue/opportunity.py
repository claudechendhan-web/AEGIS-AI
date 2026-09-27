"""The opportunity model, and the arithmetic for judging it.

An `Opportunity` is a claim that someone will pay `ask_price` for something
that costs `expected_cost` and `effort_hours` to produce. Everything else here
exists to make that claim falsifiable.

The central rule: **an opportunity must be backed by evidence retrieved from
the local index, and every claim about it must trace back to a URI.** The model
may propose opportunities freely. It may not assert that they are real without
a citation, because a plausible-sounding opportunity with no source is exactly
the input that burns a budget.

Money is `treasury.money.Money`, so the same exactness and the same
reserve-inaccessible rule apply here as everywhere else.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from revenue.errors import InvalidOpportunityError
from treasury.money import ZERO, Money

# Opportunity categories. Deliberately a closed set: an open vocabulary lets a
# model invent a fresh category for every bad idea, which would defeat the
# whole point of accumulating a track record per category.
CATEGORIES: tuple[str, ...] = (
    "data_product",
    "service",
    "tooling",
    "content",
    "integration",
    "research",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class Evidence:
    """A retrievable citation supporting an opportunity."""

    uri: str
    title: str = ""
    snippet: str = ""
    score: float = 0.0
    retrieved_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri.strip():
            raise InvalidOpportunityError("evidence uri must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "title": self.title,
            "snippet": self.snippet,
            "score": round(self.score, 6),
            "retrieved_at": self.retrieved_at,
        }


@dataclass(frozen=True, slots=True)
class Opportunity:
    """A falsifiable claim about a way to earn."""

    category: str
    summary: str
    ask_price: Money
    expected_cost: Money
    effort_hours: float
    evidence: tuple[Evidence, ...] = ()
    opportunity_id: str = field(default_factory=_new_id)
    horizon_days: int = 30
    created_at: str = field(default_factory=_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise InvalidOpportunityError(
                f"category must be one of: {', '.join(CATEGORIES)}"
            )
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise InvalidOpportunityError("summary must be a non-empty string")
        if not isinstance(self.ask_price, Money):
            raise InvalidOpportunityError("ask_price must be a Money value")
        if not isinstance(self.expected_cost, Money):
            raise InvalidOpportunityError("expected_cost must be a Money value")
        if self.ask_price.currency != self.expected_cost.currency:
            raise InvalidOpportunityError(
                "ask_price and expected_cost must share a currency"
            )
        if not self.ask_price.is_positive:
            raise InvalidOpportunityError("ask_price must be positive")
        if self.expected_cost.is_negative:
            raise InvalidOpportunityError("expected_cost must not be negative")
        if not isinstance(self.effort_hours, (int, float)) or self.effort_hours <= 0:
            raise InvalidOpportunityError("effort_hours must be positive")
        if self.horizon_days <= 0:
            raise InvalidOpportunityError("horizon_days must be positive")
        object.__setattr__(self, "evidence", tuple(self.evidence))

    @property
    def currency(self) -> str:
        return self.ask_price.currency

    @property
    def net(self) -> Money:
        """Revenue after direct costs, before valuing the agent's time."""
        return self.ask_price - self.expected_cost

    @property
    def margin_ratio(self) -> Decimal:
        """Net as a fraction of ask price."""
        if not self.ask_price.is_positive:
            return ZERO
        return self.net.amount / self.ask_price.amount

    @property
    def effective_hourly(self) -> Money:
        """Net per hour of effort. The number that actually decides.

        Rounded down via `scaled`, never constructed directly. Dividing first
        and building a Money from the quotient produces a repeating decimal
        with far more than eight places, and `Money` rejects excess precision
        rather than truncating it. Rounding down is also the conservative
        direction: it understates the rate, so a borderline opportunity is
        treated as too weak rather than strong enough.
        """
        if self.effort_hours <= 0:
            return Money.zero(self.currency)
        reciprocal = Decimal(1) / Decimal(str(self.effort_hours))
        return self.net.scaled(reciprocal)

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "category": self.category,
            "summary": self.summary,
            "ask_price": self.ask_price.to_dict(),
            "expected_cost": self.expected_cost.to_dict(),
            "net": self.net.to_dict(),
            "margin_ratio": format(self.margin_ratio, "f"),
            "effort_hours": self.effort_hours,
            "effective_hourly": self.effective_hourly.to_dict(),
            "horizon_days": self.horizon_days,
            "evidence": [item.to_dict() for item in self.evidence],
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


def opportunity_from_dict(payload: Mapping[str, Any]) -> Opportunity:
    """Rebuild an opportunity from stored JSON."""
    return Opportunity(
        opportunity_id=str(payload.get("opportunity_id") or _new_id()),
        category=str(payload["category"]),
        summary=str(payload["summary"]),
        ask_price=Money.from_dict(payload["ask_price"]),
        expected_cost=Money.from_dict(payload["expected_cost"]),
        effort_hours=float(payload["effort_hours"]),
        evidence=tuple(
            Evidence(
                uri=str(item["uri"]),
                title=str(item.get("title", "")),
                snippet=str(item.get("snippet", "")),
                score=float(item.get("score", 0.0)),
            )
            for item in payload.get("evidence", ())
        ),
        horizon_days=int(payload.get("horizon_days", 30)),
        created_at=str(payload.get("created_at") or _now()),
        metadata=dict(payload.get("metadata") or {}),
    )


def collect_evidence(
    query: str,
    *,
    limit: int = 3,
    min_score: float = 0.0,
    retriever: Any = None,
) -> tuple[Evidence, ...]:
    """Turn retrieval results into evidence.

    Accepts either a `memory.retriever.Retriever` or a `MemoryIndex`. A
    refused or empty result yields no evidence, which is the point: an
    unsubstantiated opportunity gets rejected downstream.
    """
    if retriever is None:
        return ()
    if hasattr(retriever, "retrieve"):
        context = retriever.retrieve(query, limit=limit, min_score=min_score)
        items = [
            (
                citation.get("uri", ""),
                str(citation.get("title", "")),
                hit.text[:400],
                float(citation.get("score", 0.0)),
            )
            for citation, hit in zip(context.citations, context.hits)
        ]
    else:
        hits = retriever.search(query, limit=limit, min_score=min_score)
        items = [(hit.uri, hit.title, hit.text[:400], hit.score) for hit in hits]
    return tuple(
        Evidence(uri=uri, title=title, snippet=snippet, score=score)
        for uri, title, snippet, score in items
        if uri
    )


def build_opportunity(
    category: str,
    summary: str,
    *,
    ask_price: Money,
    expected_cost: Money,
    effort_hours: float,
    query: str = "",
    horizon_days: int = 30,
    retriever: Any = None,
    metadata: Mapping[str, Any] | None = None,
) -> Opportunity:
    """Construct an opportunity, attaching evidence retrieved for `query`."""
    evidence = (
        collect_evidence(query, retriever=retriever) if retriever is not None else ()
    )
    return Opportunity(
        category=category,
        summary=summary,
        ask_price=ask_price,
        expected_cost=expected_cost,
        effort_hours=effort_hours,
        evidence=evidence,
        horizon_days=horizon_days,
        metadata=dict(metadata or {}),
    )


def summarize(opportunities: Sequence[Opportunity]) -> dict[str, Any]:
    """Aggregate view of a batch, for logging."""
    if not opportunities:
        return {"count": 0, "by_category": {}}
    by_category: dict[str, int] = {}
    for opportunity in opportunities:
        by_category[opportunity.category] = by_category.get(opportunity.category, 0) + 1
    return {
        "count": len(opportunities),
        "by_category": by_category,
        "total_ask": format(
            sum((item.ask_price.amount for item in opportunities), ZERO), "f"
        ),
        "total_net": format(
            sum((item.net.amount for item in opportunities), ZERO), "f"
        ),
        "currency": opportunities[0].currency,
    }
