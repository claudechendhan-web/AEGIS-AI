"""Tests for self-improvement.

The boundary these tests defend: the tuner may change numbers within bounds
and may write lessons, and it may do neither of the things that would actually
matter â€” granting a capability or weakening a budget rule. A self-improvement
loop that can disable its own guardrails is not an improvement loop.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from revenue.selftune import BOUNDS, Adjustment, SelfTuner, _clamp
from revenue.trackrecord import TrackRecord
from treasury import Money

VERIFICATION_WEAK = {
    "records_examined": 200,
    "buckets": {
        "verified": 28,
        "failed": 15,
        "timeout": 3,
        "unverifiable": 124,
        "no_code": 1,
        "unsafe": 29,
    },
    "verified_rate_of_executable": "0.6087",
    "executed": True,
}

VERIFICATION_STRONG = {
    "records_examined": 200,
    "buckets": {
        "verified": 190,
        "failed": 0,
        "timeout": 0,
        "unverifiable": 0,
        "no_code": 10,
        "unsafe": 0,
    },
    "verified_rate_of_executable": "0.9500",
    "executed": True,
}

MARKET_DEAD = {
    "demand_read": {"viable": False, "crowded": True},
    "aggregate": {"total_downloads": 400},
}
MARKET_LIVE = {
    "demand_read": {"viable": True, "crowded": False},
    "aggregate": {"total_downloads": 90000},
}


def usd(value: str) -> Money:
    return Money.parse(value, "USD")


@pytest.fixture()
def tuner() -> Iterator[SelfTuner]:
    with SelfTuner(":memory:") as store:
        yield store


def make_opportunity(category: str, ask: str = "20.00"):
    from revenue.opportunity import Evidence, Opportunity

    return Opportunity(
        category=category,
        summary="thing",
        ask_price=usd(ask),
        expected_cost=usd("0.10"),
        effort_hours=1.0,
        evidence=(Evidence(uri="https://market.test/x"),),
    )


# -- defaults ---------------------------------------------------------------


def test_starts_at_defaults(tuner: SelfTuner) -> None:
    assert tuner.get("curation.min_quality_score") == Decimal("0.60")
    assert tuner.get("pricing.ask_multiplier") == Decimal("1.00")


def test_an_unknown_parameter_reads_zero(tuner: SelfTuner) -> None:
    assert tuner.get("nonsense.parameter") == Decimal(0)


# -- bounds -----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(BOUNDS))
def test_every_parameter_is_bounded(tuner: SelfTuner, name: str) -> None:
    low, high = BOUNDS[name]
    assert _clamp(name, low - Decimal(1000)) == low
    assert _clamp(name, high + Decimal(1000)) == high


def test_an_out_of_band_proposal_is_clamped_on_apply(tuner: SelfTuner) -> None:
    """A proposal beyond the band cannot escape it by being applied."""
    applied = tuner.apply(
        [
            Adjustment(
                name="opportunity.min_margin",
                current=Decimal("0.35"),
                proposed=Decimal("0.0"),
                reason="trying to switch the gate off",
                evidence="",
            )
        ]
    )
    assert applied[0].proposed == BOUNDS["opportunity.min_margin"][0]
    assert tuner.get("opportunity.min_margin") == BOUNDS["opportunity.min_margin"][0]


def test_a_tuner_cannot_widen_its_own_permissions(tuner: SelfTuner) -> None:
    """It has no route to the grant store at all, by construction."""
    assert not hasattr(tuner, "grant")
    tables = {
        row[0]
        for row in tuner.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "security_grants" not in tables


# -- tuning from verification ----------------------------------------------


def test_a_poor_verification_rate_raises_the_quality_bar(tuner: SelfTuner) -> None:
    proposals = tuner.propose(verification=VERIFICATION_WEAK)
    quality = [p for p in proposals if p.name == "curation.min_quality_score"]
    if quality:
        assert quality[0].proposed > quality[0].current
        assert "unsafe" in quality[0].evidence


def test_a_strong_verification_rate_relaxes_the_bar(tuner: SelfTuner) -> None:
    proposals = tuner.propose(verification=VERIFICATION_STRONG)
    quality = [p for p in proposals if p.name == "curation.min_quality_score"]
    assert quality
    assert quality[0].proposed < quality[0].current


def test_ask_price_scales_to_what_is_actually_verified(tuner: SelfTuner) -> None:
    """Claiming full price for a 14%-verified corpus is how a listing dies."""
    proposals = tuner.propose(verification=VERIFICATION_WEAK)
    pricing = [p for p in proposals if p.name == "pricing.ask_multiplier"]
    assert pricing
    assert pricing[0].proposed < Decimal("0.30")
    assert "28 verified of 200" in pricing[0].evidence


def test_no_verification_means_no_proposal(tuner: SelfTuner) -> None:
    assert tuner.propose(verification=None) == []


def test_zero_examined_is_ignored(tuner: SelfTuner) -> None:
    assert tuner.propose(verification={"records_examined": 0, "buckets": {}}) == []


# -- tuning from the market -------------------------------------------------


def test_a_dead_market_raises_the_viability_bar(tuner: SelfTuner) -> None:
    proposals = tuner.propose(market=MARKET_DEAD)
    bar = [p for p in proposals if p.name == "market.viable_median_downloads"]
    assert bar
    assert bar[0].proposed > bar[0].current
    assert "no demand" in bar[0].reason


def test_a_live_market_relaxes_the_bar(tuner: SelfTuner) -> None:
    proposals = tuner.propose(market=MARKET_LIVE)
    bar = [p for p in proposals if p.name == "market.viable_median_downloads"]
    assert bar
    assert bar[0].proposed < bar[0].current


def test_market_without_a_read_is_ignored(tuner: SelfTuner) -> None:
    assert tuner.propose(market={"aggregate": {"total_downloads": 5}}) == []


# -- tuning from the track record -------------------------------------------


def test_a_consistently_winning_category_lowers_the_margin_floor(
    tuner: SelfTuner,
) -> None:
    with TrackRecord(":memory:") as record:
        for _ in range(3):
            opportunity = make_opportunity("integration")
            record.record_opportunity(opportunity)
            record.record_outcome(opportunity.opportunity_id, usd("20.00"))
        with SelfTuner(":memory:", track_record=record) as tuned:
            proposals = tuned.propose()
            margin = [p for p in proposals if p.name == "opportunity.min_margin"]
            assert margin
            assert margin[0].proposed < margin[0].current
            assert "integration 3/3" in margin[0].evidence


def test_no_track_record_means_no_track_tuning(tuner: SelfTuner) -> None:
    assert tuner.track_record is None
    assert not [p for p in tuner.propose() if p.name == "opportunity.min_margin"]


# -- applying and reverting -------------------------------------------------


def test_apply_persists_and_logs(tuner: SelfTuner) -> None:
    applied = tuner.apply(
        [
            Adjustment(
                name="pricing.ask_multiplier",
                current=Decimal("1.00"),
                proposed=Decimal("0.40"),
                reason="measured verification rate",
                evidence="28/200",
            )
        ]
    )
    assert applied[0].applied is True
    assert tuner.get("pricing.ask_multiplier") == Decimal("0.40")
    history = tuner.history()
    assert history[0]["name"] == "pricing.ask_multiplier"
    assert history[0]["before_value"] == format(Decimal("1.00"), "f")
    assert history[0]["after_value"] == format(Decimal("0.40"), "f")


def test_revert_restores_the_default(tuner: SelfTuner) -> None:
    tuner.apply(
        [
            Adjustment(
                name="curation.min_quality_score",
                current=Decimal("0.60"),
                proposed=Decimal("0.90"),
                reason="r",
                evidence="e",
            )
        ]
    )
    assert tuner.get("curation.min_quality_score") == Decimal("0.90")
    assert tuner.revert("curation.min_quality_score") == Decimal("0.60")
    assert tuner.get("curation.min_quality_score") == Decimal("0.60")
    assert any("reverted" in row["reason"] for row in tuner.history())


def test_tuning_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "selftune.db"
    with SelfTuner(path) as first:
        first.apply(
            [
                Adjustment(
                    name="pricing.ask_multiplier",
                    current=Decimal("1.00"),
                    proposed=Decimal("0.30"),
                    reason="r",
                    evidence="e",
                )
            ]
        )
    with SelfTuner(path) as second:
        assert second.get("pricing.ask_multiplier") == Decimal("0.30")


# -- lessons ----------------------------------------------------------------


def test_a_failing_category_produces_a_lesson() -> None:
    with TrackRecord(":memory:") as record:
        for _ in range(3):
            opportunity = make_opportunity("content")
            record.record_opportunity(opportunity)
            record.record_outcome(opportunity.opportunity_id, usd("0.00"))
        with SelfTuner(":memory:", track_record=record) as tuner:
            assert tuner.learn_from_outcomes() == ["content"]
            lessons = tuner.lessons()
            assert lessons
            assert "content" in lessons[0]["lesson"]
            assert "Do not propose it again" in lessons[0]["lesson"]


def test_a_winning_category_produces_a_preference() -> None:
    with TrackRecord(":memory:") as record:
        for _ in range(3):
            opportunity = make_opportunity("integration")
            record.record_opportunity(opportunity)
            record.record_outcome(opportunity.opportunity_id, usd("20.00"))
        with SelfTuner(":memory:", track_record=record) as tuner:
            tuner.learn_from_outcomes()
            lesson = tuner.lessons()[0]["lesson"]
            assert "integration" in lesson
            assert "prefer it" in lesson


def test_a_single_outcome_produces_no_lesson() -> None:
    with TrackRecord(":memory:") as record:
        opportunity = make_opportunity("research")
        record.record_opportunity(opportunity)
        record.record_outcome(opportunity.opportunity_id, usd("0.00"))
        with SelfTuner(":memory:", track_record=record) as tuner:
            assert tuner.learn_from_outcomes() == []
            assert tuner.lessons() == []


def test_lesson_block_is_prompt_ready() -> None:
    with TrackRecord(":memory:") as record:
        for _ in range(3):
            opportunity = make_opportunity("content")
            record.record_opportunity(opportunity)
            record.record_outcome(opportunity.opportunity_id, usd("0.00"))
        with SelfTuner(":memory:", track_record=record) as tuner:
            tuner.learn_from_outcomes()
            block = tuner.lesson_block()
            assert "already measured" in block
            assert "content" in block
            assert block.startswith("What you have")


def test_an_empty_lesson_block_is_empty() -> None:
    with SelfTuner(":memory:") as tuner:
        assert tuner.lesson_block() == ""


def test_lessons_are_capped() -> None:
    with SelfTuner(":memory:") as tuner:
        for index in range(80):
            tuner.record_lesson(f"lesson number {index}")
        assert len(tuner.lessons(limit=100)) <= 40


def test_recording_the_same_lesson_twice_replaces_it() -> None:
    with SelfTuner(":memory:") as tuner:
        tuner.record_lesson("the same thing")
        tuner.record_lesson("the   same thing")
        assert len(tuner.lessons(limit=10)) == 1


def test_an_empty_lesson_is_ignored() -> None:
    with SelfTuner(":memory:") as tuner:
        tuner.record_lesson("   ")
        assert tuner.lessons() == []


# -- reporting --------------------------------------------------------------


def test_report_shows_parameters_and_bounds(tuner: SelfTuner) -> None:
    report = tuner.to_dict()
    assert "pricing.ask_multiplier" in report["parameters"]
    assert report["bounds"]["opportunity.min_margin"] == ["0.10", "0.90"]


def test_adjustment_serialises() -> None:
    payload = Adjustment(
        name="x",
        current=Decimal(1),
        proposed=Decimal(2),
        reason="r",
        evidence="e",
    ).to_dict()
    assert payload["direction"] == "up"
    assert payload["changed"] is True
    json.dumps(payload)
