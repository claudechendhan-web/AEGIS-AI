"""Tests for listing generation.

The tool's whole promise is that a listing's claims are derived from files on
disk rather than written by hand. These tests defend that: a claim must trace
to a source, and the limitations section must not contradict the claims.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from training.generate_listing import (
    ListingError,
    build_tiers,
    find_package,
    generate_listing,
    render_markdown,
)
from treasury.money import Money


@pytest.fixture()
def package(tmp_path: Path) -> Path:
    directory = tmp_path / "product"
    directory.mkdir()
    (directory / "data-1.0.jsonl").write_text("{}\n", encoding="utf-8")
    (directory / "sample.jsonl").write_text("{}\n", encoding="utf-8")
    manifest = {
        "dataset": "aegis-curated-python",
        "version": "1.0.0",
        "domain_filter": "scientific_numerics",
        "curation": {
            "records_read": 5003,
            "records_kept": 400,
            "retention_rate": "0.0800",
        },
        "not_verified": [
            "generated code has not been executed or unit tested",
            "no human review of correctness",
        ],
        "licence": {"note": "Apache-2.0 upstream; confirm before redistribution."},
        "outputs": {"sha256": "a" * 64},
    }
    (directory / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return directory


def write_verify(directory: Path, **overrides) -> Path:
    payload = {
        "records_examined": 200,
        "buckets": {
            "verified": 28,
            "failed": 15,
            "timeout": 0,
            "unverifiable": 124,
            "no_code": 1,
            "unsafe": 29,
        },
        "verified_rate_of_executable": "0.6087",
        "executed": True,
    }
    payload.update(overrides)
    path = directory / "verify.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# -- package discovery ------------------------------------------------------


def test_find_package_ignores_the_sample(package: Path) -> None:
    assert find_package(package).name == "data-1.0.jsonl"


def test_find_package_reports_an_empty_directory(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ListingError):
        find_package(empty)


def test_missing_manifest_is_reported(tmp_path: Path) -> None:
    directory = tmp_path / "product"
    directory.mkdir()
    (directory / "data-1.0.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ListingError):
        generate_listing(directory)


def test_zero_records_is_reported(package: Path) -> None:
    manifest_path = package / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["curation"]["records_kept"] = 0
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ListingError) as info:
        generate_listing(package)
    assert "zero kept records" in str(info.value)


# -- claims -----------------------------------------------------------------


def test_every_claim_names_a_source(package: Path) -> None:
    listing = generate_listing(package, verify_path=write_verify(package))
    assert listing["claims"]
    for claim in listing["claims"]:
        assert claim["source"]
        assert claim["claim"]


def test_claims_come_from_the_manifest(package: Path) -> None:
    listing = generate_listing(package)
    text = json.dumps(listing["claims"])
    assert "400 records" in text
    assert "5,003" in text
    assert "0.0800" in text


def test_verification_numbers_appear_in_claims(package: Path) -> None:
    listing = generate_listing(package, verify_path=write_verify(package))
    text = json.dumps(listing["claims"])
    assert "28 of 200" in text
    assert "0.6087" in text


def test_market_downloads_appear_when_supplied(package: Path) -> None:
    market = package / "market.json"
    market.write_text(
        json.dumps({"aggregate": {"total_downloads": 56675}}), encoding="utf-8"
    )
    listing = generate_listing(
        package, verify_path=write_verify(package), market_path=market
    )
    assert "56,675" in json.dumps(listing["claims"])


# -- the contradiction this tool exists to prevent --------------------------


def test_limitations_never_claim_code_was_not_executed(
    package: Path,
) -> None:
    """Regression: the manifest's stale blanket note contradicted the claims.

    The manifest says "code has not been executed"; the verification report
    says 28 records executed cleanly. Printing both produced a listing that
    contradicted itself, which is the one failure mode this tool is built to
    avoid.
    """
    listing = generate_listing(package, verify_path=write_verify(package))
    text = " ".join(listing["what_is_not_claimed"]).lower()
    assert "not been executed" not in text
    assert "never executed" in text
    assert "28 of 200" in json.dumps(listing["claims"])


def test_without_verification_the_manifest_limitations_survive(
    package: Path,
) -> None:
    listing = generate_listing(package)
    text = " ".join(listing["what_is_not_claimed"])
    assert "not been executed" in text
    assert "no human review" in text


def test_dangerous_records_are_disclosed(package: Path) -> None:
    listing = generate_listing(package, verify_path=write_verify(package))
    text = " ".join(listing["what_is_not_claimed"])
    assert "29 examined records were refused" in text
    assert "dangerous patterns" in text


def test_unexamined_records_are_disclosed(package: Path) -> None:
    listing = generate_listing(package, verify_path=write_verify(package))
    text = " ".join(listing["what_is_not_claimed"])
    assert "only 200 of 400" in text
    assert "200 were never executed" in text


def test_unverifiable_is_not_called_a_failure(package: Path) -> None:
    """A missing package is an environment fact, not a correctness verdict."""
    listing = generate_listing(package, verify_path=write_verify(package))
    text = " ".join(listing["what_is_not_claimed"])
    assert "environment limitation, not a correctness verdict" in text


# -- pricing ----------------------------------------------------------------


def test_tiers_are_monotonic() -> None:
    tiers = build_tiers(400, Decimal("0.6"), "scientific_numerics")
    assert len(tiers) == 3
    amounts = [tier.price.amount for tier in tiers]
    assert amounts == sorted(amounts)
    assert amounts[0] > Decimal(0)


def test_tiers_respect_the_floor() -> None:
    tiers = build_tiers(1, Decimal(0), "python")
    assert tiers[0].price.amount == Decimal("29.00")


def test_tiers_respect_the_ceiling() -> None:
    tiers = build_tiers(10_000_000, Decimal(1), "python")
    assert tiers[2].price.amount <= Decimal("2499.00")


def test_a_higher_verification_rate_prices_higher() -> None:
    low = build_tiers(400, Decimal("0.10"), "python")[0].price.amount
    high = build_tiers(400, Decimal("0.90"), "python")[0].price.amount
    assert high > low


def test_tiers_carry_exact_money() -> None:
    for tier in build_tiers(400, Decimal("0.6"), "python"):
        assert isinstance(tier.price, Money)
        assert tier.price.amount == tier.price.amount.quantize(Decimal("0.00000001"))


def test_listing_always_says_prices_are_computed(package: Path) -> None:
    listing = generate_listing(package, verify_path=write_verify(package))
    text = " ".join(listing["what_is_not_claimed"])
    assert "computed starting point" in text
    assert "market-derived price" in text


# -- rendering --------------------------------------------------------------


def test_markdown_contains_every_section(package: Path) -> None:
    listing = generate_listing(package, verify_path=write_verify(package))
    markdown = render_markdown(listing)
    for heading in ("# ", "## What is in it", "## Pricing", "## Licence"):
        assert heading in markdown
    assert "| Personal |" in markdown
    assert "not claim" in markdown


def test_markdown_shows_price_currency(package: Path) -> None:
    listing = generate_listing(package)
    markdown = render_markdown(listing)
    assert "USD" in markdown


# -- checklist --------------------------------------------------------------


def test_checklist_requires_a_lawyer(package: Path) -> None:
    listing = generate_listing(package)
    text = " ".join(listing["publish_checklist"]).lower()
    assert "lawyer" in text
    assert "accuracy" in text
