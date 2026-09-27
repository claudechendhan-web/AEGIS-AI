"""Tests for the escalation queue.

The queue's whole purpose is to make a wall visible instead of silent, so the
tests are about two things: that real blockers are detected, and that a
repeatedly-detected blocker does not become forty requests. An inbox nobody
reads is worse than no inbox, because it teaches you to ignore it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from escalation import (
    ANSWERED,
    OPEN,
    Kind,
    Request,
    RequestQueue,
    Severity,
    detect_blockers,
    notify,
    notify_new,
)
from treasury.money import Money


@pytest.fixture()
def queue() -> Iterator[RequestQueue]:
    with RequestQueue(":memory:") as store:
        yield store


def usd(value: str) -> Money:
    return Money.parse(value, "USD")


def request(title: str = "blocked", kind: Kind = Kind.ACCOUNT) -> Request:
    return Request(
        kind=kind,
        title=title,
        detail="why it is blocked",
        unblock="do the thing",
    )


# -- the queue --------------------------------------------------------------


def test_a_filed_request_is_open(queue: RequestQueue) -> None:
    filed = queue.file(request())
    assert filed.state == OPEN
    assert len(queue.open_requests()) == 1
    assert queue.get(filed.request_id) is not None


def test_identical_blockers_are_not_duplicated(queue: RequestQueue) -> None:
    """An unattended run re-detects the same blocker every round."""
    for _ in range(20):
        queue.file(request("nothing is published"))
    assert len(queue.open_requests()) == 1


def test_different_titles_are_separate(queue: RequestQueue) -> None:
    queue.file(request("first blocker"))
    queue.file(request("second blocker"))
    assert len(queue.open_requests()) == 2


def test_same_title_different_kind_is_separate(queue: RequestQueue) -> None:
    queue.file(request("shared title", kind=Kind.ACCOUNT))
    queue.file(request("shared title", kind=Kind.PAYMENT))
    assert len(queue.open_requests()) == 2


def test_resolution_closes_the_request(queue: RequestQueue) -> None:
    filed = queue.file(request())
    updated = queue.resolve(filed.request_id, note="listed it")
    assert updated is not None
    assert updated.state == ANSWERED
    assert updated.resolution == "listed it"
    assert queue.open_requests() == []


def test_a_resolved_blocker_can_be_filed_again(queue: RequestQueue) -> None:
    filed = queue.file(request())
    queue.resolve(filed.request_id, note="done")
    # The condition may genuinely recur, so the same title must be able to
    # open a fresh request rather than being permanently suppressed.
    queue.file(request())
    assert len(queue.open_requests()) == 1


def test_resolving_an_unknown_id_returns_none(queue: RequestQueue) -> None:
    assert queue.resolve("nope") is None


def test_dismiss_closes_without_a_note(queue: RequestQueue) -> None:
    filed = queue.file(request())
    assert queue.dismiss(filed.request_id) is not None
    assert queue.open_requests() == []


def test_severity_ordering(queue: RequestQueue) -> None:
    queue.file(
        Request(
            kind=Kind.CONTACT,
            title="fyi one",
            detail="d",
            unblock="u",
            severity=Severity.FYI,
        )
    )
    queue.file(
        Request(
            kind=Kind.ACCOUNT,
            title="blocking one",
            detail="d",
            unblock="u",
            severity=Severity.BLOCKING,
        )
    )
    queue.file(
        Request(
            kind=Kind.RESOURCE,
            title="degraded one",
            detail="d",
            unblock="u",
            severity=Severity.DEGRADED,
        )
    )
    order = [item.severity for item in queue.open_requests()]
    assert order == [Severity.BLOCKING, Severity.DEGRADED, Severity.FYI]


def test_events_are_recorded(queue: RequestQueue) -> None:
    filed = queue.file(request())
    queue.resolve(filed.request_id, note="done")
    events = [event["kind"] for event in queue.events()]
    assert "answer" in events


def test_counts_reflect_state(queue: RequestQueue) -> None:
    filed = queue.file(request())
    queue.resolve(filed.request_id, note="done")
    assert queue.counts().get(ANSWERED) == 1


def test_queue_persists(tmp_path: Path) -> None:
    path = tmp_path / "escalations.db"
    with RequestQueue(path) as first:
        filed = first.file(request("persisted blocker"))
    with RequestQueue(path) as second:
        assert second.get(filed.request_id) is not None
        assert len(second.open_requests()) == 1


# -- detection --------------------------------------------------------------


def detect(**overrides):
    payload = {
        "model_available": True,
        "model_detail": "qwen2.5-coder:7b is available",
        "spendable": usd("20.00"),
        "reserve": usd("80.00"),
        "approved_pending": 0,
        "blocked_categories": {},
        "published": True,
        "licence_confirmed": True,
        "package_records": 0,
        "verified_records": 0,
    }
    payload.update(overrides)
    return detect_blockers(**payload)


def test_a_healthy_agent_has_no_blockers() -> None:
    assert detect() == []


def test_missing_model_is_degraded_not_blocking() -> None:
    found = detect(model_available=False, model_detail="no models pulled")
    assert len(found) == 1
    assert found[0].kind is Kind.RESOURCE
    assert found[0].severity is Severity.DEGRADED
    assert "ollama pull" in found[0].unblock


def test_pending_transactions_are_blocking() -> None:
    found = detect(approved_pending=3, published=False)
    payment = [item for item in found if item.kind is Kind.PAYMENT]
    assert payment
    assert payment[0].severity is Severity.BLOCKING
    # The count lives in the detail, not the title: the queue de-duplicates on
    # kind + title, so a count in the title would file a fresh copy every
    # round and defeat the dedup entirely.
    assert "3 approved opportunit" in payment[0].detail
    assert "3" not in payment[0].title
    assert "earn(" in payment[0].unblock


def test_one_pending_transaction_is_singular() -> None:
    found = detect(approved_pending=1, published=False)
    payment = next(item for item in found if item.kind is Kind.PAYMENT)
    assert "1 approved opportunity is funded" in payment.detail


def test_unpublished_package_needs_an_account() -> None:
    found = detect(published=False, package_records=400, verified_records=28)
    account = next(item for item in found if item.kind is Kind.ACCOUNT)
    assert "400" in account.detail
    assert "28 verified" in account.detail
    assert "LISTING.md" in account.unblock
    assert "will not impersonate" in account.detail


def test_licence_is_flagged_and_the_agent_refuses_to_skip_it() -> None:
    found = detect(licence_confirmed=False, package_records=400)
    legal = next(item for item in found if item.kind is Kind.LEGAL)
    assert "will not accept an instruction" in legal.detail
    assert "lawyer" in legal.unblock


def test_cleared_licence_raises_no_legal_request() -> None:
    assert not [item for item in detect() if item.kind is Kind.LEGAL]


def test_many_blocked_categories_ask_for_new_ones() -> None:
    blocked = {
        "content": "hit rate 0",
        "service": "hit rate 0",
        "research": "hit rate 0",
    }
    found = detect(blocked_categories=blocked)
    approval = [item for item in found if item.kind is Kind.APPROVAL]
    assert approval
    assert approval[0].severity is Severity.DEGRADED
    assert "content" in approval[0].detail


def test_one_blocked_category_is_not_an_approval_request() -> None:
    found = detect(blocked_categories={"content": "hit rate 0"})
    assert not [item for item in found if item.kind is Kind.APPROVAL]


def test_blockers_are_ordered_most_stopping_first() -> None:
    found = detect(
        model_available=False,
        model_detail="no models",
        approved_pending=2,
        published=False,
        package_records=400,
        licence_confirmed=False,
    )
    severities = [item.severity for item in found]
    assert severities == sorted(
        severities,
        key=lambda s: [Severity.BLOCKING, Severity.DEGRADED, Severity.FYI].index(s),
    )


def test_every_blocker_explains_itself() -> None:
    for item in detect(
        model_available=False,
        model_detail="no models",
        approved_pending=1,
        published=False,
        package_records=400,
    ):
        assert item.title
        assert item.detail
        assert item.unblock


# -- notification -----------------------------------------------------------


def test_notify_can_be_disabled() -> None:
    assert notify("t", "m", method="none") is False


def test_notify_never_raises(monkeypatch) -> None:
    """A machine with no toast support must not break the agent."""

    def explode(*args, **kwargs):
        raise OSError("no display")

    monkeypatch.setattr("subprocess.run", explode)
    assert notify("t", "m", method="auto") is False


def test_notify_new_on_an_empty_queue_sends_nothing(queue: RequestQueue) -> None:
    assert notify_new(queue, method="none") == 0


def test_notify_new_prefers_blocking(queue: RequestQueue) -> None:
    sent: list[str] = []

    def fake(title: str, message: str, **kwargs) -> bool:
        sent.append(title)
        return True

    import escalation.requests as module

    module.notify = fake  # type: ignore[assignment]
    queue.file(
        Request(
            kind=Kind.CONTACT,
            title="just so you know",
            detail="d",
            unblock="u",
            severity=Severity.FYI,
        )
    )
    queue.file(
        Request(
            kind=Kind.PAYMENT,
            title="needs a transaction",
            detail="d",
            unblock="u",
            severity=Severity.BLOCKING,
        )
    )
    assert notify_new(queue, method="auto") == 1
    assert "needs a transaction" in sent[0]


# -- serialisation ----------------------------------------------------------


def test_request_serializes() -> None:
    payload = request().to_dict()
    assert payload["kind"] == "account"
    assert payload["kind_label"]
    assert payload["state"] == OPEN
    assert payload["resolution"] == ""


def test_context_survives_a_round_trip(queue: RequestQueue) -> None:
    queue.file(
        Request(
            kind=Kind.PAYMENT,
            title="with context",
            detail="d",
            unblock="u",
            context={"amount": "20.00", "count": 3},
        )
    )
    stored = queue.open_requests()[0]
    assert stored.context["count"] == 3
    assert stored.context["amount"] == "20.00"


def test_decimal_and_money_context_survives(queue: RequestQueue) -> None:
    queue.file(
        Request(
            kind=Kind.PAYMENT,
            title="decimal context",
            detail="d",
            unblock="u",
            context={"spendable": usd("1.25")},
        )
    )
    stored = queue.open_requests()[0]
    assert "1.25" in str(stored.context["spendable"])
