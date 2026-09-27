"""Tests for the market probe.

The probe's job is to report demand evidence without overclaiming it, so the
tests are mostly about the boundaries: a failed probe must not be reported as
an empty market, and no code path may turn downloads into a revenue claim.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from revenue.market import (
    DEFAULT_CAVEATS,
    DemandSignal,
    Listing,
    MarketProbe,
    MarketProbeError,
    MarketReport,
    opportunity_from_market,
    probe_market,
)


def listing(identifier: str, downloads: int, likes: int = 0) -> Listing:
    return Listing(identifier=identifier, downloads=downloads, likes=likes)


class _Response:
    def __init__(self, payload: object):
        self._payload = json.dumps(payload).encode()
        self.status = 200
        self.headers = {"Content-Type": "application/json"}

    def read(self, size: int = -1) -> bytes:
        return self._payload

    def close(self) -> None:
        pass


class _Opener:
    def __init__(self, pages: dict[str, object], fail: bool = False):
        self.pages = pages
        self.fail = fail
        self.requested: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requested.append(url)
        if self.fail:
            from urllib.error import URLError

            raise URLError("network down")
        for key, payload in self.pages.items():
            if key in url:
                return _Response(payload)
        return _Response([])


# -- parsing ----------------------------------------------------------------


def test_search_parses_listings() -> None:
    opener = _Opener(
        {
            "numpy": [
                {"id": "a/b", "downloads": 500, "likes": 12, "tags": ["x"]},
                {"id": "c/d", "downloads": 20, "likes": 1, "tags": []},
            ]
        }
    )
    signal = MarketProbe(opener=opener).search("numpy")
    assert signal.count == 2
    assert signal.total_downloads == 520
    assert signal.top is not None
    assert signal.top.identifier == "a/b"


def test_search_tolerates_missing_fields() -> None:
    opener = _Opener({"q": [{"id": "a/b"}, {"nope": 1}, "not a dict"]})
    signal = MarketProbe(opener=opener).search("q")
    assert signal.count == 1


def test_search_handles_an_empty_platform_response() -> None:
    signal = MarketProbe(opener=_Opener({"q": []})).search("q")
    assert signal.count == 0
    assert signal.median_downloads == Decimal(0)
    assert signal.top is None


def test_search_handles_a_non_list_payload() -> None:
    signal = MarketProbe(opener=_Opener({"q": {"unexpected": True}})).search("q")
    assert signal.count == 0


# -- statistics -------------------------------------------------------------


def test_median_of_odd_count() -> None:
    signal = DemandSignal(
        query="q",
        listings=(listing("a", 10), listing("b", 20), listing("c", 30)),
    )
    assert signal.median_downloads == Decimal(20)


def test_median_of_even_count() -> None:
    signal = DemandSignal(
        query="q",
        listings=(
            listing("a", 10),
            listing("b", 21),
            listing("c", 30),
            listing("d", 41),
        ),
    )
    assert signal.median_downloads == Decimal("25.5")


def test_engagement_weights_likes() -> None:
    assert listing("a", 100, likes=1).engagement == 110
    assert listing("a", 0, likes=50).engagement == 500


# -- failures ---------------------------------------------------------------


def test_a_network_failure_raises() -> None:
    with pytest.raises(MarketProbeError):
        MarketProbe(opener=_Opener({}, fail=True)).search("q")


def test_all_probes_failing_raises_rather_than_reporting_no_market() -> None:
    """Regression risk: silence must not read as "no demand"."""
    with pytest.raises(MarketProbeError):
        probe_market(["a", "b"], probe=MarketProbe(opener=_Opener({}, fail=True)))


def test_a_partial_failure_is_reported_but_not_fatal() -> None:
    def opener(request, timeout=None):
        if "flaky" in request.full_url:
            from urllib.error import URLError

            raise URLError("flaky")
        return _Response([{"id": "a/b", "downloads": 500, "likes": 2}])

    report = probe_market(
        ["good", "flaky"], probe=MarketProbe(opener=opener), viable_median=100
    )
    assert report.viable is True
    assert any("flaky" in reason for reason in report.reasons)


def test_probe_requires_at_least_one_query() -> None:
    with pytest.raises(MarketProbeError):
        probe_market([])


# -- the demand read --------------------------------------------------------


def test_viable_market_is_reported_viable() -> None:
    opener = _Opener({"q": [{"id": f"d{i}", "downloads": 4000} for i in range(3)]})
    report = probe_market(["q"], probe=MarketProbe(opener=opener), viable_median=1000)
    assert report.viable is True


def test_a_long_tail_market_is_not_viable() -> None:
    """One dominant listing and a long tail is not a viable median market."""
    payload = [{"id": "star", "downloads": 37908}] + [
        {"id": f"d{i}", "downloads": 5} for i in range(19)
    ]
    report = probe_market(
        ["q"], probe=MarketProbe(opener=_Opener({"q": payload})), viable_median=1000
    )
    assert report.viable is False
    assert any(
        "long tail" in reason or "below the" in reason for reason in report.reasons
    )


def test_crowding_is_detected() -> None:
    payload = [{"id": f"d{i}", "downloads": 9000} for i in range(40)]
    report = probe_market(
        ["q"],
        probe=MarketProbe(opener=_Opener({"q": payload})),
        crowded_at=25,
    )
    assert report.crowded is True


def test_caveats_are_always_attached() -> None:
    report = probe_market(["q"], probe=MarketProbe(opener=_Opener({"q": []})))
    assert report.caveats == DEFAULT_CAVEATS


def test_report_never_claims_revenue() -> None:
    opener = _Opener({"q": [{"id": "a/b", "downloads": 99999, "likes": 500}]})
    report = probe_market(["q"], probe=MarketProbe(opener=opener), viable_median=1)
    payload = json.dumps(report.to_dict()).lower()
    assert "downloads are not revenue" in payload
    assert "will sell" not in payload


def test_report_serializes() -> None:
    opener = _Opener({"q": [{"id": "a/b", "downloads": 10, "likes": 1}]})
    report = probe_market(["q"], probe=MarketProbe(opener=opener), viable_median=1)
    payload = report.to_dict()
    assert payload["aggregate"]["listings_found"] == 1
    assert payload["demand_read"]["viable"] is True


# -- opportunity construction -----------------------------------------------


def test_viable_market_yields_evidence_backed_opportunity() -> None:
    from treasury.money import Money

    opener = _Opener({"q": [{"id": "a/b", "downloads": 9000, "likes": 20}]})
    report = probe_market(["q"], probe=MarketProbe(opener=opener), viable_median=100)
    opportunity = opportunity_from_market(
        report,
        ask_price=Money.parse("200.00", "USD"),
        build_cost=Money.parse("10.00", "USD"),
        effort_hours=5.0,
    )
    assert opportunity.evidence
    assert "huggingface.co/datasets/a/b" in opportunity.evidence[0].uri
    assert opportunity.metadata["market_viable"] is True


def test_a_market_with_no_demand_cannot_become_an_opportunity() -> None:
    """The probe must not manufacture a bet out of a dead market."""
    from treasury.money import Money

    opener = _Opener({"q": [{"id": "d", "downloads": 1}]})
    report = probe_market(["q"], probe=MarketProbe(opener=opener), viable_median=10000)
    with pytest.raises(MarketProbeError) as info:
        opportunity_from_market(
            report,
            ask_price=Money.parse("200.00", "USD"),
            build_cost=Money.parse("1.00", "USD"),
            effort_hours=1.0,
        )
    assert "no demand" in str(info.value)


def test_empty_report_is_constructible() -> None:
    report = MarketReport(signals=(), viable=False, crowded=False)
    assert report.total_downloads == 0
    assert report.to_dict()["aggregate"]["listings_found"] == 0
