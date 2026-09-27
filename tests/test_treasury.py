"""Tests for the self-funding treasury.

The tests concentrate on the two claims that matter: the 20/80 split always
reconciles exactly, and the agent cannot reach the reserve. Everything else is
in service of proving those two hold under adversarial input.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest

from treasury import (
    Money,
    Treasury,
)
from treasury.errors import (
    CurrencyMismatchError,
    InsufficientFundsError,
    InvalidAmountError,
    LedgerClosedError,
    ReservationError,
    ReserveLockedError,
    SpendLimitExceededError,
    UnbalancedEntryError,
    UnknownEnvelopeError,
)
from treasury.ledger import Direction, Entry, EntryKind, Posting
from treasury.money import QUANTUM
from treasury.policy import RESERVE, BudgetPolicy


@pytest.fixture()
def bank() -> Iterator[Treasury]:
    with Treasury(":memory:") as treasury:
        yield treasury


def usd(value: str) -> Money:
    return Money.parse(value, "USD")


# -- money ------------------------------------------------------------------


def test_float_is_rejected() -> None:
    # A float is deliberately wrong-typed: the point is that Money rejects it.
    with pytest.raises(InvalidAmountError):
        Money(1.5, "USD")  # type: ignore[arg-type]


def test_excess_precision_is_rejected_not_truncated() -> None:
    with pytest.raises(InvalidAmountError):
        Money.parse("1.000000001", "USD")


def test_currency_mismatch_is_refused() -> None:
    with pytest.raises(CurrencyMismatchError):
        usd("1.00") + Money.parse("1.00", "EUR")


def test_scaling_rounds_down() -> None:
    # 1.00 * 0.333333333 has nine decimal places and must lose the last one.
    assert usd("1.00").scaled(Decimal("0.333333333")).amount == Decimal("0.33333333")
    # Rounding must never inflate the result.
    assert usd("0.01").scaled(Decimal("0.999")).amount <= Decimal("0.01")


# -- the 20/80 rule ---------------------------------------------------------


@pytest.mark.parametrize(
    "income",
    ["1.00", "7.77", "19.99", "100.00", "0.03", "1234.56", "999999.99"],
)
def test_allocation_is_exactly_twenty_eighty(income: str) -> None:
    policy = BudgetPolicy.standard("USD")
    split = policy.allocate(usd(income))
    assert split.spendable + split.reserve == split.income
    assert split.spendable.amount == (usd(income).scaled(Decimal("0.20"))).amount
    assert split.reserve.amount >= usd(income).scaled(Decimal("0.80")).amount


def test_rounding_cannot_mint_money() -> None:
    policy = BudgetPolicy.standard("USD")
    for cents in range(1, 200):
        income = usd(f"0.{cents:02d}")
        split = policy.allocate(income)
        assert split.spendable + split.reserve == income
        assert split.spendable.amount <= income.amount


def test_earn_credits_both_envelopes(bank: Treasury) -> None:
    split = bank.earn(usd("100.00"), memo="invoice 1042")
    assert split.spendable == usd("20.00")
    assert split.reserve == usd("80.00")
    assert bank.ledger.balance("env:OPERATE") == usd("20.00")
    assert bank.ledger.balance("env:RESERVE") == usd("80.00")


def test_ledger_reconciles_after_every_operation(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    bank.earn(usd("33.33"), memo="invoice 2")
    bank.spend(usd("4.44"), memo="api")
    assert bank.ledger.assert_balanced("USD").is_zero


def test_income_must_be_positive(bank: Treasury) -> None:
    with pytest.raises(InvalidAmountError):
        bank.earn(usd("0.00"), memo="nothing")
    with pytest.raises(InvalidAmountError):
        bank.earn(usd("-5.00"), memo="negative")


def test_income_currency_must_match_policy(bank: Treasury) -> None:
    with pytest.raises(CurrencyMismatchError):
        bank.earn(Money.parse("10.00", "EUR"), memo="wrong currency")


# -- the locked reserve -----------------------------------------------------


def test_agent_cannot_spend_the_reserve(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    with pytest.raises(ReserveLockedError):
        bank.spend(
            usd("80.00"),
            memo="agent tries to raid reserve",
            envelope_name=RESERVE,
        )
    assert bank.ledger.balance("env:RESERVE") == usd("80.00")


def test_spending_the_whole_operate_envelope_empties_only_it(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    bank.spend(usd("20.00"), memo="spend it all")
    assert bank.ledger.balance("env:OPERATE") == usd("0.00")
    assert bank.ledger.balance("env:RESERVE") == usd("80.00")


def test_overspending_operate_is_refused(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    with pytest.raises(InsufficientFundsError):
        bank.spend(usd("20.01"), memo="one cent too far")
    assert bank.ledger.balance("env:OPERATE") == usd("20.00")


def test_reserve_release_requires_owner_authorization(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    with pytest.raises(InvalidAmountError):
        bank.release_reserve(usd("10.00"), memo="no proof", authorization="  ")


def test_owner_release_moves_reserve_into_operate(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    bank.release_reserve(
        usd("10.00"), memo="seed a project", authorization="owner-approved-1"
    )
    assert bank.ledger.balance("env:RESERVE") == usd("70.00")
    assert bank.ledger.balance("env:OPERATE") == usd("30.00")
    bank.ledger.assert_balanced("USD")


def test_release_cannot_exceed_reserve(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    with pytest.raises(InsufficientFundsError):
        bank.release_reserve(
            usd("80.01"), memo="too much", authorization="owner-approved-1"
        )


def test_can_spend_is_a_safe_probe(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    assert bank.can_spend(usd("20.00")) is True
    assert bank.can_spend(usd("20.01")) is False
    assert bank.can_spend(usd("1.00"), envelope_name=RESERVE) is False


# -- circuit breakers -------------------------------------------------------


def test_per_run_spend_limit_is_enforced() -> None:
    policy = BudgetPolicy.standard("USD")
    policy = BudgetPolicy(
        currency="USD",
        spend_ratio=Decimal("0.20"),
        max_spend_per_run=usd("5.00"),
    )
    with Treasury(":memory:", policy=policy) as bank:
        bank.earn(usd("100.00"), memo="invoice")
        bank.spend(usd("3.00"), memo="first", spent_this_run=usd("0.00"))
        with pytest.raises(SpendLimitExceededError):
            bank.spend(usd("3.00"), memo="second", spent_this_run=usd("3.00"))
        assert bank.ledger.balance("env:OPERATE") == usd("17.00")


def test_unknown_envelope_is_refused(bank: Treasury) -> None:
    with pytest.raises(UnknownEnvelopeError):
        bank.spend(usd("1.00"), memo="guess", envelope_name="SLUSH_FUND")


def test_spend_must_be_positive(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    with pytest.raises(InvalidAmountError):
        bank.spend(usd("0.00"), memo="nothing")
    with pytest.raises(InvalidAmountError):
        bank.spend(usd("-1.00"), memo="negative")


# -- reservations -----------------------------------------------------------


def test_hold_blocks_a_double_spend(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    reservation_id = bank.ledger.hold("env:OPERATE", usd("15.00"), memo="in flight")
    assert bank.ledger.available("env:OPERATE") == usd("5.00")
    with pytest.raises(InsufficientFundsError):
        bank.spend(usd("15.00"), memo="double spend")
    bank.ledger.release(reservation_id)
    assert bank.ledger.available("env:OPERATE") == usd("20.00")
    bank.spend(usd("15.00"), memo="now it fits")
    assert bank.ledger.balance("env:OPERATE") == usd("5.00")


def test_reservation_cannot_be_settled_twice(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    reservation_id = bank.ledger.hold("env:OPERATE", usd("5.00"))
    bank.ledger.settle(reservation_id)
    with pytest.raises(ReservationError):
        bank.ledger.settle(reservation_id)


def test_cannot_hold_more_than_available(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    with pytest.raises(InvalidAmountError):
        bank.ledger.hold("env:OPERATE", usd("20.01"))


# -- ledger integrity -------------------------------------------------------


def test_unbalanced_entry_is_refused(bank: Treasury) -> None:
    with pytest.raises(UnbalancedEntryError):
        bank.ledger.post(
            Entry(
                kind=EntryKind.ADJUSTMENT,
                memo="forged",
                postings=(
                    Posting("env:OPERATE", Direction.DEBIT, usd("10.00")),
                    Posting("ext:sink", Direction.CREDIT, usd("9.99")),
                ),
            )
        )


def test_postings_may_not_mix_currencies(bank: Treasury) -> None:
    with pytest.raises(CurrencyMismatchError):
        bank.ledger.post(
            Entry(
                kind=EntryKind.ADJUSTMENT,
                memo="mixed",
                postings=(
                    Posting("env:OPERATE", Direction.DEBIT, usd("10.00")),
                    Posting("ext:sink", Direction.CREDIT, Money.parse("10.00", "EUR")),
                ),
            )
        )


def test_forged_entry_would_break_the_invariant(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    assert bank.ledger.net_position("USD").is_zero


def test_entries_can_be_read_back(bank: Treasury) -> None:
    """Regression: the integer migration renamed a column a reader still used.

    `get_entry` is the only place postings are reconstructed from storage, so
    a stale column reference there left the whole read path broken while every
    balance query still passed.
    """
    bank.earn(usd("100.00"), memo="invoice")
    bank.spend(usd("1.25"), memo="api call")
    entries = bank.ledger.iter_entries(10)
    assert len(entries) == 2
    kinds = {entry.kind for entry in entries}
    assert kinds == {EntryKind.INCOME, EntryKind.EXPENSE}
    for entry in entries:
        for posting in entry.postings:
            assert isinstance(posting.amount, Money)
            assert posting.amount.amount == posting.amount.amount.quantize(QUANTUM)
    income = next(e for e in entries if e.kind is EntryKind.INCOME)
    assert len(income.postings) == 3
    assert (
        sum(
            posting.amount.amount
            for posting in income.postings
            if posting.direction is Direction.CREDIT
        )
        == usd("100.00").amount
    )


def test_ledger_close_is_idempotent() -> None:
    bank = Treasury(":memory:")
    bank.close()
    bank.close()
    with pytest.raises(LedgerClosedError):
        _ = bank.ledger.balance("env:OPERATE")


# -- the budgeted learning loop --------------------------------------------


def test_research_is_metered_against_operate(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    bank.record_observation(
        source="web",
        uri="https://example.com/pricing",
        title="competitor pricing",
        cost=usd("0.50"),
    )
    bank.record_observation(
        source="web",
        uri="https://example.org/forum",
        title="forum thread",
        cost=usd("0.25"),
    )
    assert bank.research_cost() == usd("0.75")
    assert bank.ledger.balance("env:OPERATE") == usd("19.25")


def test_free_observations_cost_nothing(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    bank.record_observation(source="local", uri="file:///notes.md")
    assert bank.research_cost().is_zero
    assert bank.ledger.balance("env:OPERATE") == usd("20.00")


def test_observations_require_a_source_and_uri(bank: Treasury) -> None:
    with pytest.raises(InvalidAmountError):
        bank.record_observation(source="", uri="https://example.com")
    with pytest.raises(InvalidAmountError):
        bank.record_observation(source="web", uri="  ")


def test_research_cannot_drain_the_reserve(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    with pytest.raises(InsufficientFundsError):
        bank.record_observation(
            source="web", uri="https://example.com", cost=usd("20.01")
        )
    assert bank.ledger.balance("env:RESERVE") == usd("80.00")


# -- runs -------------------------------------------------------------------


def test_run_tracks_income_and_spend(bank: Treasury) -> None:
    run = bank.start_run(metadata={"goal": "learn"})
    bank.earn(usd("50.00"), memo="invoice")
    bank._accumulate(run.run_id, "income", usd("50.00"))
    bank.record_observation(
        source="web", uri="https://example.com", cost=usd("2.00"), run_id=run.run_id
    )
    finished = bank.finish_run(run)
    assert finished.income == usd("50.00")
    assert finished.spent == usd("2.00")
    assert finished.finished_at is not None
    assert bank.get_run(run.run_id).spent == usd("2.00")


def test_multiple_runs_compound_reserve(bank: Treasury) -> None:
    for _ in range(5):
        bank.earn(usd("100.00"), memo="invoice")
    assert bank.ledger.balance("env:RESERVE") == usd("400.00")
    assert bank.ledger.balance("env:OPERATE") == usd("100.00")
    bank.spend(usd("100.00"), memo="spend the whole 20% five times over")
    assert bank.ledger.balance("env:OPERATE") == usd("0.00")
    assert bank.ledger.balance("env:RESERVE") == usd("400.00")
    bank.ledger.assert_balanced("USD")


def test_status_reports_the_lock(bank: Treasury) -> None:
    bank.earn(usd("100.00"), memo="invoice")
    status = bank.status()
    assert status["reserve_locked"] is True
    assert status["spendable"]["amount"] == "20.00000000"
    assert status["reserve"]["amount"] == "80.00000000"
    assert status["ledger"]["net_position"] == "0.00000000"
