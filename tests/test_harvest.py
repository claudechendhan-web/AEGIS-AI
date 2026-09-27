"""Tests for the harvest worker.

The worker is the one component that runs unattended, so these tests lean
heavily on failure behaviour: deadline, budget exhaustion, robots refusal,
resumability after a crash, and the guarantee that the reserve is never
touched. Network access is faked throughout; nothing here reaches the internet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harvest.errors import HarvestError, SourceConfigError
from harvest.frontier import DONE, FAILED, SKIPPED, Frontier
from harvest.worker import (
    HarvestConfig,
    HarvestWorker,
    load_status,
)
from memory.store import MemoryIndex
from treasury import Money, Treasury

PAGE = (
    b"<html><head><title>Doc Page</title></head><body>"
    b"<h1>Heading</h1><p>The reserve ratio is eighty percent of income.</p>"
    b"<a href='/next'>next</a></body></html>"
)


class _Response:
    def __init__(self, body: bytes = PAGE, status: int = 200, ctype: str = "text/html"):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": ctype}

    def read(self, size: int = -1) -> bytes:
        return self._body if size is None or size < 0 else self._body[:size]

    def close(self) -> None:
        pass


class _Opener:
    """Serves robots.txt, then any URL, defaulting to a valid page.

    `pages` maps a URL to an explicit response; anything else gets a
    successful page, so tests only have to describe the URLs that misbehave.
    """

    def __init__(self, robots: str | None = None, pages: dict | None = None):
        self.robots = robots
        self.pages = pages or {}
        self.requested: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requested.append(url)
        if url.endswith("/robots.txt"):
            if self.robots is None:
                return _Response(b"", ctype="text/plain", status=404)
            return _Response(self.robots.encode(), ctype="text/plain")
        return self.pages.get(url, _Response())


@pytest.fixture()
def paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "index": tmp_path / "memory.db",
        "frontier": tmp_path / "harvest.db",
        "status": tmp_path / "status.json",
        "treasury": tmp_path / "treasury.db",
    }


def config(paths: dict[str, Path], **overrides) -> HarvestConfig:
    payload: dict = {
        "sources": ("https://docs.test/a.html",),
        "deadline_hours": 1.0,
        "max_pages": 10,
        "max_depth": 1,
        "cycle_pages": 5,
        "delay_seconds": 0.0,
        "index_path": paths["index"],
        "frontier_path": paths["frontier"],
        "status_path": paths["status"],
        "settle_seconds": 0.0,
    }
    payload.update(overrides)
    return HarvestConfig(**payload)


def worker_for(paths, opener, *, treasury=None, **overrides) -> HarvestWorker:
    """Build a worker whose fetches are served by `opener`.

    The per-fetch cost is left to the config; overriding it here would hide
    the very accounting bug this file guards against.
    """
    worker = HarvestWorker(
        config(paths, **overrides),
        index=MemoryIndex(":memory:"),
        frontier=Frontier(":memory:"),
        treasury=treasury,
        sleep=lambda _: None,
    )
    original = worker._build_ingestor

    def build():
        ingestor = original()
        ingestor._opener = opener
        return ingestor

    worker._build_ingestor = build  # type: ignore[method-assign]
    return worker


# -- configuration ----------------------------------------------------------


def test_config_requires_a_positive_deadline() -> None:
    with pytest.raises(SourceConfigError):
        HarvestConfig(deadline_hours=0)
    with pytest.raises(SourceConfigError):
        HarvestConfig(deadline_hours=-1)


def test_config_requires_at_least_one_source() -> None:
    with pytest.raises(SourceConfigError):
        HarvestConfig(sources=("   ",))


def test_config_rejects_negative_limits() -> None:
    with pytest.raises(SourceConfigError):
        HarvestConfig(max_depth=-1)
    with pytest.raises(SourceConfigError):
        HarvestConfig(max_pages=0)
    with pytest.raises(SourceConfigError):
        HarvestConfig(cycle_pages=0)
    with pytest.raises(SourceConfigError):
        HarvestConfig(delay_seconds=-1)


# -- frontier ---------------------------------------------------------------


def test_frontier_is_deduplicated() -> None:
    with Frontier(":memory:") as frontier:
        assert frontier.add(["https://a.test/x", "https://a.test/x"]) == 1
        assert frontier.add(["https://a.test/x"]) == 0
        assert frontier.pending_estimate() == 1


def test_frontier_ignores_non_http_urls() -> None:
    with Frontier(":memory:") as frontier:
        assert frontier.add(["ftp://a.test/x", "mailto:a@b.test", "not a url"]) == 0


def test_take_claims_and_increments_attempts() -> None:
    with Frontier(":memory:", max_attempts=2) as frontier:
        frontier.add(["https://a.test/x"])
        claimed = frontier.take(limit=1)
        assert len(claimed) == 1
        assert claimed[0].attempts == 1
        # Still claimable on a retry: that is what makes an interrupted run
        # resume rather than abandon. The worker marks each URL as it goes, so
        # nothing is processed twice within one pass.
        assert len(frontier.take(limit=1)) == 1
        assert frontier.take(limit=1) == []


def test_exhausted_urls_become_failed_not_forever_pending() -> None:
    """A URL that ran out of attempts must not linger as pending.

    It can never be claimed again, so leaving it `pending` would overstate
    remaining work and make a finished run look resumable.
    """
    with Frontier(":memory:", max_attempts=2) as frontier:
        frontier.add(["https://a.test/x"])
        frontier.take(limit=1)
        frontier.take(limit=1)
        # The reclassification is lazy: it happens on the next claim pass.
        assert frontier.take(limit=1) == []
        assert frontier.pending_estimate() == 0
        assert frontier.counts().get(FAILED) == 1
        assert "exhausted" in frontier.iter_failures()[0]["last_error"]


def test_take_respects_max_attempts() -> None:
    with Frontier(":memory:", max_attempts=2) as frontier:
        frontier.add(["https://a.test/x"])
        assert len(frontier.take(limit=1)) == 1
        assert len(frontier.take(limit=1)) == 1
        assert frontier.take(limit=1) == []


def test_take_skips_backed_off_hosts() -> None:
    with Frontier(":memory:") as frontier:
        frontier.add(["https://bad.test/x", "https://good.test/y"])
        frontier.penalize_host("bad.test")
        claimed = frontier.take(limit=5)
        assert [item.host for item in claimed] == ["good.test"]


def test_mark_and_counts() -> None:
    with Frontier(":memory:") as frontier:
        frontier.add(["https://a.test/x", "https://a.test/y"])
        frontier.take(limit=2)
        frontier.mark("https://a.test/x", DONE)
        frontier.mark("https://a.test/y", FAILED, "boom")
        counts = frontier.counts()
        assert counts.get(DONE) == 1
        assert counts.get(FAILED) == 1
        assert frontier.iter_failures()[0]["last_error"] == "boom"


def test_mark_rejects_an_unknown_state() -> None:
    with Frontier(":memory:") as frontier:
        frontier.add(["https://a.test/x"])
        with pytest.raises(HarvestError):
            frontier.mark("https://a.test/x", "confused")


def test_frontier_survives_reopen(paths: dict[str, Path]) -> None:
    with Frontier(paths["frontier"]) as frontier:
        frontier.add(["https://a.test/x"])
    with Frontier(paths["frontier"]) as frontier:
        assert frontier.pending_estimate() == 1


# -- happy path -------------------------------------------------------------


def test_worker_indexes_a_page(paths: dict[str, Path]) -> None:
    opener = _Opener(robots="User-agent: *\nAllow: /")
    worker = worker_for(paths, opener)
    stats = worker.run()
    assert stats.indexed == 1
    assert stats.chunks > 0
    assert stats.stopped_reason in ("frontier_empty", "max_pages", "deadline")
    assert worker.index.stats().documents == 1
    assert worker.index.search("reserve ratio")


def test_worker_follows_links_within_depth(paths: dict[str, Path]) -> None:
    opener = _Opener(
        robots="User-agent: *\nAllow: /",
        pages={
            "https://docs.test/a.html": _Response(),
            "https://docs.test/next": _Response(
                b"<html><body><p>Page two content here.</p>"
                b"<a href='/a.html'>back</a></body></html>"
            ),
        },
    )
    worker = worker_for(paths, opener, max_depth=1, max_pages=2)
    worker.run()
    assert worker.frontier.counts().get(DONE, 0) >= 1


def test_worker_does_not_follow_links_when_disabled(paths: dict[str, Path]) -> None:
    opener = _Opener(robots="User-agent: *\nAllow: /")
    worker = worker_for(paths, opener, follow_links=False, max_pages=1)
    worker.run()
    assert worker.frontier.pending_estimate() == 0


# -- the refusals -----------------------------------------------------------


def test_robots_refusal_is_counted_not_hidden(paths: dict[str, Path]) -> None:
    opener = _Opener(robots="User-agent: *\nDisallow: /a.html")
    worker = worker_for(paths, opener)
    stats = worker.run()
    assert stats.refused == 1
    assert stats.indexed == 0
    assert worker.index.stats().documents == 0
    assert worker.frontier.counts().get(SKIPPED) == 1


def test_network_failure_is_recorded_with_its_error(paths: dict[str, Path]) -> None:
    opener = _Opener(
        robots="User-agent: *\nAllow: /",
        pages={
            "https://docs.test/a.html": _Response(b"", ctype="text/plain", status=500)
        },
    )
    worker = worker_for(paths, opener)
    worker.run()
    failures = worker.frontier.iter_failures()
    assert failures
    assert "500" in failures[0]["last_error"]


def test_budget_exhaustion_stops_the_worker(paths: dict[str, Path]) -> None:
    with Treasury(paths["treasury"]) as bank:
        bank.earn(Money.parse("0.00000001", "USD"), memo="almost nothing")
        opener = _Opener(robots="User-agent: *\nAllow: /")
        worker = worker_for(paths, opener, treasury=bank, cost_per_fetch=1.0)
        stats = worker.run()
        assert stats.stopped_reason == "budget"
        assert stats.indexed == 0


def test_empty_body_is_skipped_not_indexed(paths: dict[str, Path]) -> None:
    opener = _Opener(
        robots="User-agent: *\nAllow: /",
        pages={"https://docs.test/a.html": _Response(b"<html><body></body></html>")},
    )
    worker = worker_for(paths, opener)
    stats = worker.run()
    assert stats.skipped == 1
    assert worker.index.stats().documents == 0


# -- treasury integration ---------------------------------------------------


def test_fetching_spends_the_treasury_and_never_touches_reserve(
    paths: dict[str, Path],
) -> None:
    with Treasury(paths["treasury"]) as bank:
        bank.earn(Money.parse("10.00", "USD"), memo="funding")
        opener = _Opener(robots="User-agent: *\nAllow: /")
        worker = worker_for(paths, opener, treasury=bank, cost_per_fetch=0.01)
        worker.run()
        operate = bank.ledger.balance("env:OPERATE")
        reserve = bank.ledger.balance("env:RESERVE")
        assert operate < Money.parse("10.00", "USD")
        assert reserve == Money.parse("8.00", "USD")


def test_reported_spend_matches_the_treasury(
    paths: dict[str, Path],
) -> None:
    """The run total must equal what the treasury actually charged.

    Regression: spending lowers the envelope balance, so computing the delta
    as `after - before` yields a negative number which a `> 0` guard then
    discards. The run reported `0.00000000` spent while every request was
    genuinely paid for.
    """
    with Treasury(paths["treasury"]) as bank:
        bank.earn(Money.parse("10.00", "USD"), memo="funding")
        before = bank.ledger.balance("env:OPERATE")
        opener = _Opener(robots="User-agent: *\nAllow: /")
        worker = worker_for(
            paths, opener, treasury=bank, cost_per_fetch=0.01, max_pages=3
        )
        stats = worker.run()
        charged = before - bank.ledger.balance("env:OPERATE")
        assert charged > Money.parse("0.00", "USD")
        assert stats.spent == format(charged.amount, "f")


# -- the bounded guarantees -------------------------------------------------


def test_max_pages_is_respected(paths: dict[str, Path]) -> None:
    opener = _Opener(robots="User-agent: *\nAllow: /")
    worker = worker_for(paths, opener, max_pages=3, max_depth=2)
    stats = worker.run()
    assert stats.attempted <= 3


def test_deadline_stops_the_run(paths: dict[str, Path]) -> None:
    from harvest.worker import Deadline

    deadline = Deadline(hours=0.0)
    assert deadline.expired()
    assert deadline.remaining() == 0.0
    assert not deadline.stopping


def test_deadline_respects_the_settle_reserve(paths: dict[str, Path]) -> None:
    from harvest.worker import Deadline

    generous = Deadline(hours=1.0, settle_seconds=0.0)
    assert not generous.expired(reserve=1800.0)
    assert generous.expired(reserve=7200.0)


def test_deadline_honours_the_stop_flag() -> None:
    from harvest.worker import Deadline

    deadline = Deadline(hours=1.0)
    deadline._stop = True
    assert deadline.stopping


def test_inflight_urls_are_restored_on_stop(paths: dict[str, Path]) -> None:
    """A stop must not swallow the tail of the queue."""
    with Frontier(":memory:") as frontier:
        frontier.add([f"https://docs.test/{index}.html" for index in range(5)])
        worker = HarvestWorker(
            config(paths, max_pages=5),
            index=MemoryIndex(":memory:"),
            frontier=frontier,
            sleep=lambda _: None,
        )
        from harvest.worker import Deadline

        deadline = Deadline(hours=1.0)
        deadline._stop = True
        worker._cycle(worker._build_ingestor(), deadline, budget=5)
        # Everything is still claimable for a later run.
        assert frontier.pending_estimate() == 5


# -- resumability -----------------------------------------------------------


def test_a_second_run_resumes_and_does_not_refetch(
    paths: dict[str, Path],
) -> None:
    opener = _Opener(robots="User-agent: *\nAllow: /")
    with Frontier(paths["frontier"]) as first:
        worker = HarvestWorker(
            config(paths),
            index=MemoryIndex(paths["index"]),
            frontier=first,
            sleep=lambda _: None,
        )
        original = worker._build_ingestor

        def build():
            ingestor = original()
            ingestor._opener = opener
            return ingestor

        worker._build_ingestor = build  # type: ignore[method-assign]
        worker.run()
        first_requests = len(opener.requested)

    with Frontier(paths["frontier"]) as second:
        worker = HarvestWorker(
            config(paths),
            index=MemoryIndex(paths["index"]),
            frontier=second,
            sleep=lambda _: None,
        )
        original = worker._build_ingestor

        def build():
            ingestor = original()
            ingestor._opener = opener
            return ingestor

        worker._build_ingestor = build  # type: ignore[method-assign]
        stats = worker.run()
        assert stats.attempted == 0
        assert len(opener.requested) == first_requests


# -- status -----------------------------------------------------------------


def test_status_file_is_written_and_readable(paths: dict[str, Path]) -> None:
    opener = _Opener(robots="User-agent: *\nAllow: /")
    worker = worker_for(paths, opener)
    worker.run()
    assert paths["status"].exists()
    payload = load_status(paths["status"])
    assert payload is not None
    assert payload["index"]["documents"] == 1
    assert payload["stats"]["indexed"] == 1
    assert payload["config"]["deadline_hours"] == 1.0


def test_status_file_is_valid_json_even_while_running(
    paths: dict[str, Path],
) -> None:
    opener = _Opener(robots="User-agent: *\nAllow: /")
    worker = worker_for(paths, opener)
    worker._write_status()
    json.loads(paths["status"].read_text(encoding="utf-8"))


def test_load_status_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_status(tmp_path / "nothing.json") is None


def test_load_status_tolerates_corruption(tmp_path: Path) -> None:
    target = tmp_path / "broken.json"
    target.write_text("{not json", encoding="utf-8")
    assert load_status(target) is None


def test_status_reports_the_treasury(paths: dict[str, Path]) -> None:
    with Treasury(paths["treasury"]) as bank:
        bank.earn(Money.parse("5.00", "USD"), memo="funding")
        opener = _Opener(robots="User-agent: *\nAllow: /")
        worker = worker_for(paths, opener, treasury=bank)
        worker._write_status()
        payload = load_status(paths["status"])
        assert payload is not None
        assert payload["treasury"]["reserve_locked"] is True
        assert payload["treasury"]["reserve"]["amount"] == "4.00000000"


# -- events -----------------------------------------------------------------


def test_events_are_logged(paths: dict[str, Path]) -> None:
    opener = _Opener(robots="User-agent: *\nAllow: /")
    worker = worker_for(paths, opener)
    worker.run()
    events = [event["event"] for event in worker.frontier.recent_events(50)]
    assert "seed" in events
    assert "indexed" in events
    assert "stop" in events
