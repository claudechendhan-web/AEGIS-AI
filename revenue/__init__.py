"""Revenue evaluation for the AEGIS-X agent.

This package does not teach the agent to make money. Nothing here can, and any
package that claims to is fiction. What it does is narrower and real:

* An **opportunity** is a falsifiable claim: someone pays `ask_price`, it costs
  `expected_cost` and `effort_hours` to deliver.
* It must be **backed by evidence** retrieved from the local index. A claim with
  no citation is refused, not scored.
* It is **scored against the treasury's real budget**, so the reserve is never
  treated as money available to spend.
* A **track record** accumulates what actually paid, and a category that
  measurably does not pay stops being proposed. That feedback loop is the only
  thing here that learns anything, and it can only make the agent more
  conservative, never more reckless.

Layout:

* `revenue.opportunity` - the model, the cost arithmetic, and evidence capture.
* `revenue.evaluate`   - the gates, which refuse rather than warn.
* `revenue.trackrecord` - durable outcomes and the per-category kill rule.

Typical use:

    from revenue import Money, OpportunityEvaluator, TrackRecord, build_opportunity

    record = TrackRecord("data/revenue.db")
    evaluator = OpportunityEvaluator(treasury=bank, track_record=record)

    candidate = build_opportunity(
        "data_product",
        "Curated Python instruction set, licence per use",
        ask_price=Money.parse("200.00", "USD"),
        expected_cost=Money.parse("12.50", "USD"),
        effort_hours=6.0,
        query="curated python dataset licence price",
        retriever=retriever,
    )

    verdict = evaluator.evaluate(candidate)
    if verdict.approved:
        evaluator.commit(verdict)
        record.record_outcome(candidate.opportunity_id, Money.parse("200.00", "USD"))

Why this and not a scraper that "finds opportunities on the internet": a scraper
produces a list of things that sound profitable, and acting on that list is
how a budget disappears. Retrieval supplies *evidence*; the gates decide
whether the evidence plus the arithmetic justify spending. The agent can
propose anything. It cannot proceed on nothing.
"""

from revenue.errors import (
    InvalidOpportunityError,
    NoAffordabilityError,
    RevenueError,
    UnknownOpportunityError,
    UnsubstantiatedOpportunityError,
)
from revenue.evaluate import (
    VERDICT_REJECTED,
    VERDICT_WORTHWHILE,
    EvaluationPolicy,
    GateResult,
    OpportunityEvaluator,
    Verdict,
    strict_evaluator,
)
from revenue.opportunity import (
    CATEGORIES,
    Evidence,
    Opportunity,
    build_opportunity,
    collect_evidence,
    opportunity_from_dict,
    summarize,
)
from revenue.trackrecord import (
    CategoryPolicy,
    CategoryStats,
    TrackRecord,
    policy_from_dict,
)

__all__ = [
    "CATEGORIES",
    "VERDICT_REJECTED",
    "VERDICT_WORTHWHILE",
    "CategoryPolicy",
    "CategoryStats",
    "EvaluationPolicy",
    "Evidence",
    "GateResult",
    "InvalidOpportunityError",
    "NoAffordabilityError",
    "Opportunity",
    "OpportunityEvaluator",
    "RevenueError",
    "TrackRecord",
    "UnknownOpportunityError",
    "UnsubstantiatedOpportunityError",
    "Verdict",
    "build_opportunity",
    "collect_evidence",
    "opportunity_from_dict",
    "policy_from_dict",
    "strict_evaluator",
    "summarize",
]
