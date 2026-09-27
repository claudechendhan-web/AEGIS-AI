"""Tests for the revenue evaluation subsystem.

The tests concentrate on the refusals. A gate that approves too readily is
worse than no gate at all, because it spends real money from the treasury, so
each gate gets a test that it actually blocks.

The track-record tests are mostly about one thing: an opportunity that was
evaluated but never resolved is not evidence of anything, and counting it as
an attempt lets an idle loop kill every category and stop all work.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from memory.retriever import Retriever
from memory.store import MemoryIndex
from revenue import (
    CATEGORIES,
    CategoryPolicy,
    EvaluationPolicy,
    Evidence,
    GateResult,
    InvalidOpportunityError,
    NoAffordabilityError,
    Opportunity,
    OpportunityEvaluator,
    RevenueError,
    TrackRecord,
    UnknownOpportunityError,
    UnsubstantiatedOpportunityError,
    Verdict,
    build_opportunity,
    collect_evidence,
    opportunity_from_dict,
    strict_evaluator,
    summarize,
)
from treasury import Money, Treasury

USD = "USD"


def usd(value: str) -> Money:
    return Money.parse(value, USD)


def evidence(count: int = 1) -> tuple[Evidence, ...]:
    return tuple(
        Evidence(uri=f"https://source.test/{index}", title=f"Source {index}")
        for index in range(count)
    )


def make(
    ask: str = "200.00",
    cost: str = "10.00",
    hours: float = 4.0,
    *,
    category: str = "service",
    items: tuple[Evidence, ...] | None = None,
    horizon: int = 30,
) -> Opportunity:
    return Opportunity(
        category=category,
        summary="A deliverable thing",
        ask_price=usd(ask),
        expected_cost=usd(cost),
        effort_hours=hours,
        evidence=evidence() if items is None else items,
        horizon_days=horizon,
    )


@pytest.fixture()
def bank() -> Iterator[Treasury]:
    with Treasury(":memory:") as treasury:
        treasury.earn(usd("1000.00"), memo="funding")
        yield treasury


@pytest.fixture()
def record() -> Iterator[TrackRecord]:
    with TrackRecord(":memory:") as track:
        yield track


# -- the model --------------------------------------------------------------


def test_net_and_margin() -> None:
    opportunity = make(ask="200.00", cost="50.00", hours=2.0)
    assert opportunity.net == usd("150.00")
    assert opportunity.margin_ratio == Decimal("0.75")
    assert opportunity.effective_hourly == usd("75.00")


def test_effective_hourly_rounds_down() -> None:
    """188.00 over 6 hours is 31.3333..., which must not raise."""
    opportunity = make(ask="200.00", cost="12.00", hours=6.0)
    assert opportunity.effective_hourly == usd("31.33333333")


def test_invalid_opportunities_are_refused() -> None:
    with pytest.raises(InvalidOpportunityError):
        Opportunity("nonsense", "x", usd("1.00"), usd("0.00"), 1.0)
    with pytest.raises(InvalidOpportunityError):
        Opportunity("service", "  ", usd("1.00"), usd("0.00"), 1.0)
    with pytest.raises(InvalidOpportunityError):
        Opportunity("service", "x", usd("0.00"), usd("0.00"), 1.0)
    with pytest.raises(InvalidOpportunityError):
        Opportunity("service", "x", usd("1.00"), usd("0.00"), 0.0)
    with pytest.raises(InvalidOpportunityError):
        Opportunity("service", "x", usd("1.00"), usd("0.00"), 1.0, horizon_days=0)


def test_mixed_currencies_are_refused() -> None:
    with pytest.raises(InvalidOpportunityError):
        Opportunity("service", "x", usd("1.00"), Money.parse("1.00", "EUR"), 1.0)


def test_round_trips_through_dict() -> None:
    original = make(items=evidence(2))
    restored = opportunity_from_dict(original.to_dict())
    assert restored.opportunity_id == original.opportunity_id
    assert restored.net == original.net
    assert len(restored.evidence) == 2


def test_category_vocabulary_is_closed() -> None:
    assert "service" in CATEGORIES
    with pytest.raises(InvalidOpportunityError):
        Opportunity("crypto_bots", "x", usd("1.00"), usd("0.00"), 1.0)


# -- gates ------------------------------------------------------------------


def _refused(verdict: Verdict) -> GateResult:
    """The gate that refused. Fails the test if none did."""
    failure = verdict.first_failure
    assert failure is not None
    return failure


def test_no_evidence_is_refused() -> None:
    verdict = OpportunityEvaluator().evaluate(make(items=()))
    assert not verdict.approved
    assert _refused(verdict).name == "evidence"


def test_distinct_sources_are_counted_not_citations() -> None:
    duplicated = (
        Evidence(uri="https://a.test/1"),
        Evidence(uri="https://a.test/1"),
    )
    verdict = OpportunityEvaluator(policy=EvaluationPolicy(min_evidence=2)).evaluate(
        make(items=duplicated)
    )
    assert not verdict.approved


def test_margin_floor_blocks_thin_profit() -> None:
    verdict = OpportunityEvaluator(
        policy=EvaluationPolicy(min_margin_ratio=Decimal("0.50"))
    ).evaluate(make(ask="100.00", cost="60.00", hours=1.0))
    assert not verdict.approved
    assert _refused(verdict).name == "margin"


def test_hourly_floor_blocks_low_rate() -> None:
    verdict = OpportunityEvaluator(
        policy=EvaluationPolicy(min_hourly=Decimal(100))
    ).evaluate(make(ask="200.00", cost="0.00", hours=10.0))
    assert not verdict.approved
    assert _refused(verdict).name == "effective_hourly"


def test_negative_net_is_refused() -> None:
    verdict = OpportunityEvaluator().evaluate(
        make(ask="10.00", cost="50.00", hours=1.0)
    )
    assert not verdict.approved
    assert _refused(verdict).name == "positive_net"


def test_slow_payback_is_refused() -> None:
    verdict = OpportunityEvaluator(
        policy=EvaluationPolicy(max_horizon_days=30)
    ).evaluate(make(horizon=365))
    assert not verdict.approved
    assert _refused(verdict).name == "payback_horizon"


def test_reserve_cannot_fund_work(bank: Treasury) -> None:
    # 200 spendable, 800 reserved.
    verdict = OpportunityEvaluator(treasury=bank).evaluate(
        make(ask="9000.00", cost="5000.00", hours=5.0)
    )
    assert not verdict.approved
    assert _refused(verdict).name == "affordable"
    assert "reserve" in _refused(verdict).detail


def test_a_well_evidenced_opportunity_is_approved(
    bank: Treasury, record: TrackRecord
) -> None:
    evaluator = OpportunityEvaluator(treasury=bank, track_record=record)
    verdict = evaluator.evaluate(make(ask="200.00", cost="10.00", hours=4.0))
    assert verdict.approved, verdict.reason
    assert record.has_opportunity(verdict.opportunity.opportunity_id)


def test_judging_is_free_but_committing_costs(bank: Treasury) -> None:
    evaluator = OpportunityEvaluator(treasury=bank)
    opportunity = make(ask="200.00", cost="25.00", hours=4.0)
    for _ in range(5):
        evaluator.evaluate(opportunity)
    assert bank.ledger.balance("env:OPERATE") == usd("200.00")
    evaluator.commit(evaluator.evaluate(opportunity))
    assert bank.ledger.balance("env:OPERATE") == usd("175.00")


def test_committing_a_rejected_opportunity_raises(bank: Treasury) -> None:
    evaluator = OpportunityEvaluator(treasury=bank)
    verdict = evaluator.evaluate(make(items=()))
    with pytest.raises(NoAffordabilityError):
        evaluator.commit(verdict)


def test_commit_re_checks_evidence_even_if_the_verdict_says_approved(
    bank: Treasury,
) -> None:
    """Defence in depth: a hand-built approved verdict is still refused."""
    unevidenced = make(items=())
    forged = Verdict(
        opportunity=unevidenced,
        gates=(GateResult("evidence", True, "asserted by hand"),),
    )
    with pytest.raises(UnsubstantiatedOpportunityError):
        OpportunityEvaluator(treasury=bank).commit(forged)
    assert bank.ledger.balance("env:OPERATE") == usd("200.00")


def test_strict_evaluator_is_demanding(bank: Treasury) -> None:
    evaluator = strict_evaluator(treasury=bank)
    assert not evaluator.evaluate(make(items=evidence(1))).approved
    assert evaluator.evaluate(
        make(ask="400.00", cost="10.00", hours=4.0, items=evidence(3))
    ).approved


# -- evidence ---------------------------------------------------------------


def test_evidence_comes_from_the_index() -> None:
    index = MemoryIndex(":memory:")
    index.add_document(
        "https://a.test/page",
        "A curated python instruction set licence costs 200 dollars per seat. " * 8,
        title="Pricing",
    )
    found = collect_evidence("curated dataset licence price", retriever=index)
    assert found
    assert found[0].uri == "https://a.test/page"
    assert found[0].score > 0


def test_no_retriever_yields_no_evidence() -> None:
    assert collect_evidence("anything") == ()


def test_unmatched_query_yields_no_evidence() -> None:
    index = MemoryIndex(":memory:")
    index.add_document("https://a.test/x", "reserve ratio eighty percent " * 8)
    assert collect_evidence("kubernetes ingress", retriever=index) == ()


def test_build_opportunity_attaches_evidence() -> None:
    index = MemoryIndex(":memory:")
    index.add_document("https://a.test/p", "python dataset licence 200 dollars " * 8)
    opportunity = build_opportunity(
        "data_product",
        "Curated dataset",
        ask_price=usd("200.00"),
        expected_cost=usd("5.00"),
        effort_hours=2.0,
        query="python dataset licence",
        retriever=index,
    )
    assert opportunity.evidence


def test_end_to_end_with_a_real_retriever() -> None:
    index = MemoryIndex(":memory:")
    index.add_document(
        "https://market.test/rates",
        "Curated python instruction datasets licence for 200 dollars per seat. "
        "Enterprise licences start at 2000 dollars. " * 6,
        title="Market rates",
    )
    evaluator = OpportunityEvaluator()
    good = build_opportunity(
        "data_product",
        "Curated python instruction set, per-seat licence",
        ask_price=usd("200.00"),
        expected_cost=usd("12.00"),
        effort_hours=6.0,
        query="curated python dataset licence price",
        retriever=Retriever(index),
    )
    assert evaluator.evaluate(good).approved
    guess = build_opportunity(
        "service",
        "Definitely a paying market, trust me",
        ask_price=usd("5000.00"),
        expected_cost=usd("0.00"),
        effort_hours=1.0,
        query="a topic nothing in the index mentions",
        retriever=Retriever(index),
    )
    assert not evaluator.evaluate(guess).approved


# -- the track record -------------------------------------------------------


def test_outcomes_require_a_recorded_opportunity(record: TrackRecord) -> None:
    with pytest.raises(UnknownOpportunityError):
        record.record_outcome("never-existed", usd("10.00"))


def test_negative_realized_revenue_is_refused(record: TrackRecord) -> None:
    opportunity = make()
    record.record_opportunity(opportunity)
    with pytest.raises(RevenueError):
        record.record_outcome(opportunity.opportunity_id, usd("-1.00"))


def test_hit_rate_counts_clearing_the_ask(record: TrackRecord) -> None:
    for realized in ("200.00", "150.00", "0.00"):
        opportunity = make(ask="200.00", cost="0.00", hours=1.0)
        record.record_opportunity(opportunity)
        record.record_outcome(opportunity.opportunity_id, usd(realized))
    stats = record.category_stats("service")
    assert stats.attempts == 3
    # Only the first cleared the ask price.
    assert stats.wins == 1
    assert stats.hit_rate == Decimal(1) / Decimal(3)


def test_repeated_evaluation_does_not_manufacture_losses(
    record: TrackRecord,
) -> None:
    """Regression: an evaluation is not an attempt.

    An unattended loop re-evaluates the same ideas every round. If evaluation
    counted as an attempt, each round would add an attempt with zero wins, the
    hit rate would fall, and after `min_attempts` rounds *every* category
    would be killed and all work would stop silently. The observed failure was
    exactly that: blocked categories climbing 1 -> 1 -> 3 across three idle
    rounds with no outcome ever recorded.
    """
    for _ in range(10):
        record.record_opportunity(
            make(category="data_product", ask="20.00", cost="0.50", hours=1.0)
        )
    stats = record.category_stats("data_product")
    assert stats.pending == 10
    assert stats.attempts == 0
    assert stats.hit_rate == Decimal(0)
    assert stats.killed is False
    assert "pending" in stats.reason
    assert "data_product" in record.allowed_categories()


def test_only_resolved_outcomes_count(record: TrackRecord) -> None:
    for index in range(4):
        candidate = make(category="service", ask="20.00", cost="0.50", hours=1.0)
        record.record_opportunity(candidate)
        if index < 2:
            record.record_outcome(candidate.opportunity_id, usd("20.00"))
    stats = record.category_stats("service")
    assert stats.attempts == 2
    assert stats.pending == 2
    assert stats.resolved == 4
    assert stats.wins == 2
    assert stats.hit_rate == Decimal(1)
    assert not stats.killed


def test_pending_does_not_rescue_a_failing_category(record: TrackRecord) -> None:
    """The fix must not weaken the kill rule for genuinely failed work."""
    for _ in range(3):
        candidate = make(category="content", ask="20.00", cost="0.50", hours=1.0)
        record.record_opportunity(candidate)
        record.record_outcome(candidate.opportunity_id, usd("0.00"))
    for _ in range(5):
        record.record_opportunity(
            make(category="content", ask="20.00", cost="0.50", hours=1.0)
        )
    stats = record.category_stats("content")
    assert stats.killed is True
    assert "hit rate" in stats.reason
    assert "content" not in record.allowed_categories()


def test_a_category_is_killed_after_enough_failures(record: TrackRecord) -> None:
    for _ in range(3):
        opportunity = make(category="content", ask="20.00", cost="0.50", hours=1.0)
        record.record_opportunity(opportunity)
        record.record_outcome(opportunity.opportunity_id, usd("0.00"))
    stats = record.category_stats("content")
    assert stats.killed
    assert "hit rate" in stats.reason
    assert "content" in record.blocked_categories()
    assert "content" not in record.allowed_categories()


def test_a_single_failure_does_not_kill(record: TrackRecord) -> None:
    opportunity = make(category="research", ask="20.00", cost="0.50", hours=1.0)
    record.record_opportunity(opportunity)
    record.record_outcome(opportunity.opportunity_id, usd("0.00"))
    stats = record.category_stats("research")
    assert not stats.killed
    assert "insufficient data" in stats.reason


def test_a_winning_category_stays_allowed(record: TrackRecord) -> None:
    for _ in range(3):
        opportunity = make(category="tooling", ask="20.00", cost="0.50", hours=1.0)
        record.record_opportunity(opportunity)
        record.record_outcome(opportunity.opportunity_id, usd("20.00"))
    assert not record.category_stats("tooling").killed
    assert "tooling" in record.allowed_categories()


def test_kill_gate_refuses_new_work(bank: Treasury, record: TrackRecord) -> None:
    evaluator = OpportunityEvaluator(treasury=bank, track_record=record)
    for _ in range(3):
        losing = make(category="content", ask="20.00", cost="0.50", hours=1.0)
        record.record_opportunity(losing)
        record.record_outcome(losing.opportunity_id, usd("0.00"))
    fresh = make(category="content", ask="500.00", cost="1.00", hours=2.0)
    verdict = evaluator.evaluate(fresh)
    assert not verdict.approved
    assert _refused(verdict).name == "track_record"


def test_raising_the_bar_can_kill_a_previously_healthy_category(
    record: TrackRecord,
) -> None:
    # Two of three cleared the ask: healthy at a 0.2 floor, dead at 0.9.
    for realized in ("20.00", "20.00", "5.00"):
        opportunity = make(category="integration", ask="20.00", cost="0.50", hours=1.0)
        record.record_opportunity(opportunity)
        record.record_outcome(opportunity.opportunity_id, usd(realized))
    assert not record.category_stats("integration").killed
    assert "integration" in record.allowed_categories()
    record.set_policy(
        "integration", CategoryPolicy(min_hit_rate=Decimal("0.9"), min_attempts=3)
    )
    assert record.category_stats("integration").killed
    assert "integration" in record.blocked_categories()


def test_max_net_loss_survives_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "revenue.db"
    with TrackRecord(path) as first:
        first.set_policy("research", CategoryPolicy(max_net_loss=usd("50.00")))
    with TrackRecord(path) as second:
        assert second.policy_for("research").max_net_loss == usd("50.00")


def test_unrecorded_opportunities_stay_out_of_history(
    bank: Treasury, record: TrackRecord
) -> None:
    evaluator = OpportunityEvaluator(treasury=bank, track_record=record)
    evaluator.evaluate(make(items=()))
    assert record.history() == []


def test_cumulative_loss_can_kill_a_category(record: TrackRecord) -> None:
    record.set_policy(
        "data_product",
        CategoryPolicy(
            min_hit_rate=Decimal("0.0"),
            min_attempts=3,
            max_net_loss=usd("100.00"),
        ),
    )
    for _ in range(4):
        opportunity = make(
            category="data_product", ask="10.00", cost="50.00", hours=1.0
        )
        record.record_opportunity(opportunity)
        record.record_outcome(opportunity.opportunity_id, usd("0.00"))
    stats = record.category_stats("data_product")
    assert stats.net < usd("-100.00")
    assert stats.killed
    assert "loss limit" in stats.reason


def test_history_is_readable(record: TrackRecord) -> None:
    opportunity = make()
    record.record_opportunity(opportunity)
    record.record_outcome(opportunity.opportunity_id, usd("200.00"))
    history = record.history()
    assert history[0]["opportunity_id"] == opportunity.opportunity_id
    assert history[0]["realized"]["amount"] == "200.00000000"


def test_history_covers_unresolved_work(tmp_path: Path) -> None:
    """An approved-but-unresolved opportunity must still be auditable."""
    with TrackRecord(tmp_path / "revenue.db") as record:
        record.record_opportunity(make())
        assert record.history()[0]["realized"] is None


def test_all_stats_covers_every_category(record: TrackRecord) -> None:
    assert {stats.category for stats in record.all_stats()} == set(CATEGORIES)


# -- reporting --------------------------------------------------------------


def test_summarize_handles_empty_and_populated() -> None:
    assert summarize([])["count"] == 0
    summary = summarize(
        [make(ask="100.00", cost="10.00"), make(ask="50.00", cost="5.00")]
    )
    assert summary["count"] == 2
    assert summary["total_net"] == "135.00000000"
    assert summary["currency"] == USD


def test_evaluator_persists_across_sessions(tmp_path: Path) -> None:
    path = tmp_path / "revenue.db"
    with TrackRecord(path) as first:
        opportunity = make(category="content", ask="20.00", cost="0.50", hours=1.0)
        first.record_opportunity(opportunity)
        first.record_outcome(opportunity.opportunity_id, usd("0.00"))
    with TrackRecord(path) as second:
        assert second.has_opportunity(opportunity.opportunity_id)
        assert second.category_stats("content").attempts == 1
