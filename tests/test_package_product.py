"""Tests for the product packaging tool.

The tool's whole value is that its claims are checkable, so the tests assert
the claims: the manifest must account for every record, the drop reasons must
sum correctly, and the licence warning must never be silently dropped.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from training.package_product import (
    DOMAINS,
    Deduper,
    PackageStats,
    build_package,
    classify,
    score_record,
)

GOOD_NUMERIC = '''from collections.abc import Sequence

import numpy as np


def moving_average(values: Sequence[float], window: int = 3) -> np.ndarray:
    """Return the moving average of a 1-D array.

    >>> moving_average([1, 2, 3, 4]).tolist()
    [2.0, 3.0]
    """
    if window <= 0:
        raise ValueError("window must be positive")
    array = np.asarray(values, dtype=float)
    if array.size < window:
        raise ValueError("not enough samples for the requested window")
    return np.convolve(array, np.ones(window) / window, mode="valid")


if __name__ == "__main__":
    print(moving_average([1, 2, 3, 4]))
'''

THIN_RESPONSE = "def f():\n    return 1\n"


def record(identifier: str, instruction: str, response: str) -> str:
    return json.dumps(
        {
            "id": identifier,
            "messages": [
                {"role": "user", "content": instruction},
                {"role": "assistant", "content": response},
            ],
            "metadata": {},
        }
    )


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    path = tmp_path / "source.jsonl"
    lines = []
    for index in range(12):
        lines.append(
            record(
                f"n{index}",
                f"Implement a numpy simulation routine number {index} that "
                "computes a convolution over a numeric array and plots it.",
                GOOD_NUMERIC + f"\n# variant {index}\n",
            )
        )
    # Off-domain, too short, and duplicate records, so the filters have work.
    for index in range(4):
        lines.append(
            record(
                f"w{index}",
                f"Write a kubernetes deployment manifest number {index} for "
                "a cluster with docker images and a terraform module.",
                "apiVersion: apps/v1\nkind: Deployment\n"
                "spec:\n  replicas: 3\n  template:\n    spec:\n"
                "      containers:\n        - name: app\n"
                "          image: example/app:1.0\n"
                "          ports:\n            - containerPort: 8080\n"
                "      # variant\n" * 3,
            )
        )
    for index in range(5):
        lines.append(record(f"s{index}", "tiny prompt", THIN_RESPONSE))
    # An exact duplicate of n0.
    lines.append(lines[0])
    # A malformed line that must be skipped, not fatal.
    lines.append("{not json")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# -- classification ---------------------------------------------------------


def test_numeric_code_lands_in_scientific() -> None:
    assert classify("use numpy convolution", GOOD_NUMERIC) == "scientific_numerics"


def test_kubernetes_code_lands_in_data_pipeline_or_nowhere() -> None:
    domain = classify(
        "deploy with docker kubernetes terraform", "kind: Deployment\n" * 8
    )
    # Either a domain bucket or None; what matters is that it is not scientific.
    assert domain != "scientific_numerics"


def test_unclassifiable_text_returns_none() -> None:
    assert classify("hello", "hi there") is None


def test_a_record_matches_at_most_one_domain() -> None:
    domain = classify("numpy pandas csv", GOOD_NUMERIC)
    assert isinstance(domain, str)
    assert domain in DOMAINS


# -- scoring ----------------------------------------------------------------


def test_a_well_formed_response_scores_well() -> None:
    value, signals = score_record("compute a moving average", GOOD_NUMERIC)
    assert value >= Decimal("0.85")
    assert "has_code_structure" in signals
    assert "has_error_handling" in signals
    assert "has_type_hints" in signals
    assert "has_docstring" in signals


def test_a_thin_response_scores_low() -> None:
    value, _signals = score_record("do a thing", THIN_RESPONSE)
    assert value < Decimal("0.60")


def test_score_never_exceeds_one() -> None:
    value, _ = score_record("x", GOOD_NUMERIC * 3)
    assert value <= Decimal("1.00")


# -- deduper ----------------------------------------------------------------


def test_exact_duplicates_are_caught() -> None:
    deduper = Deduper(Decimal("0.80"))
    assert deduper.is_duplicate("a b c d e f g h") is False
    assert deduper.is_duplicate("a b c d e f g h") is True


def test_similar_text_is_caught() -> None:
    deduper = Deduper(Decimal("0.80"))
    base = " ".join(f"word{index}" for index in range(40))
    deduper.is_duplicate(base)
    assert deduper.is_duplicate(base + " extra tail words here") is True


def test_distinct_text_passes() -> None:
    deduper = Deduper(Decimal("0.80"))
    assert deduper.is_duplicate("alpha beta gamma delta epsilon zeta") is False
    assert (
        deduper.is_duplicate("completely unrelated content about pottery glazes")
        is False
    )


# -- stats ------------------------------------------------------------------


def test_retention_rate_is_readable() -> None:
    """Regression: raw Decimal division emitted 28 significant digits."""
    stats = PackageStats(read=5003, kept=400)
    assert stats.to_dict()["retention_rate"] == "0.0800"


def test_retention_rate_handles_an_empty_read() -> None:
    assert PackageStats().to_dict()["retention_rate"] == "0.0000"


# -- end to end -------------------------------------------------------------


def test_package_is_written_with_a_full_accounting(
    source: Path, tmp_path: Path
) -> None:
    output = tmp_path / "pkg"
    manifest = build_package(
        source=source, output=output, dataset_id="test", version="9.9"
    )
    curation = manifest["curation"]
    assert curation["records_read"] > 0
    assert curation["records_kept"] > 0
    # Every record is either kept or dropped for exactly one stated reason.
    assert (
        curation["records_kept"] + curation["records_dropped"]
        == curation["records_read"]
    )
    assert (output / "MANIFEST.json").exists()
    assert (output / "README.md").exists()
    assert (output / "sample.jsonl").exists()
    assert (output / "test-9.9.jsonl").exists()


def test_domain_filter_keeps_one_domain(source: Path, tmp_path: Path) -> None:
    manifest = build_package(
        source=source, output=tmp_path / "p", domain="scientific_numerics"
    )
    assert set(manifest["curation"]["kept_by_domain"]) <= {"scientific_numerics"}


def test_duplicate_record_is_dropped(source: Path, tmp_path: Path) -> None:
    manifest = build_package(
        source=source,
        output=tmp_path / "p",
        domain="scientific_numerics",
        near_duplicate=Decimal("0.80"),
    )
    assert manifest["curation"]["dropped_by_reason"].get("near_duplicate", 0) >= 1


def test_manifest_states_what_was_not_verified(source: Path, tmp_path: Path) -> None:
    manifest = build_package(source=source, output=tmp_path / "p")
    assert manifest["not_verified"]
    readme = (tmp_path / "p" / "README.md").read_text(encoding="utf-8")
    assert "has NOT been verified" in readme
    assert "public" in readme.lower()
    assert "not been executed" in readme


def test_manifest_keeps_the_licence_warning(source: Path, tmp_path: Path) -> None:
    manifest = build_package(source=source, output=tmp_path / "p")
    assert "Apache-2.0" in manifest["licence"]["note"]
    assert "lawyer" in manifest["licence"]["note"]


def test_records_carry_their_score_and_signals(source: Path, tmp_path: Path) -> None:
    output = tmp_path / "p"
    build_package(source=source, output=output, dataset_id="test", version="1.0")
    lines = (output / "test-1.0.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines
    parsed = json.loads(lines[0])
    assert parsed["messages"][0]["role"] == "user"
    assert parsed["messages"][1]["role"] == "assistant"
    metadata = parsed["metadata"]
    assert Decimal(metadata["quality_score"]) >= Decimal("0.60")
    assert metadata["signals"]
    assert metadata["source_id"]


def test_sample_is_smaller_than_the_full_set(source: Path, tmp_path: Path) -> None:
    output = tmp_path / "p"
    manifest = build_package(source=source, output=output, sample_size=3)
    full = len(
        (output / "aegis-curated-python-0.1.0.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    sample = len((output / "sample.jsonl").read_text(encoding="utf-8").splitlines())
    assert sample <= 3
    assert sample <= full
    assert manifest["outputs"]["sample_records"] == sample


def test_manifest_carries_a_content_hash(source: Path, tmp_path: Path) -> None:
    manifest = build_package(source=source, output=tmp_path / "p")
    assert len(manifest["outputs"]["sha256"]) == 64


def test_min_score_is_honoured(source: Path, tmp_path: Path) -> None:
    permissive = build_package(
        source=source, output=tmp_path / "a", min_score=Decimal("0.10")
    )
    strict = build_package(
        source=source, output=tmp_path / "b", min_score=Decimal("0.99")
    )
    assert strict["curation"]["records_kept"] <= permissive["curation"]["records_kept"]


def test_unknown_domain_is_rejected(source: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_package(source=source, output=tmp_path / "p", domain="crypto")


def test_missing_source_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_package(source=tmp_path / "nope.jsonl", output=tmp_path / "p")


def test_max_records_bounds_the_output(source: Path, tmp_path: Path) -> None:
    manifest = build_package(source=source, output=tmp_path / "p", max_records=3)
    assert manifest["curation"]["records_kept"] <= 3
