"""Tests for the self-improvement dataset builder.

The load-bearing tests are the secret scanner and the filter's reasons: a
fine-tune built on leaked credentials teaches the model to emit them, and a
silent drop would hide the problem.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from training.self_improve import (
    BuildStats,
    Pair,
    QualityConfig,
    SelfImprovementError,
    build,
    deduplicate,
    extract_pairs,
    filter_pair,
    normalize_for_dedup,
    redact_secrets,
    scan_secrets,
    split_records,
)


def make_pair(instruction: str, response: str, record_id: str = "m1") -> Pair:
    return Pair(
        record_id=record_id,
        instruction=instruction,
        response=response,
        conversation_id="c1",
        created_at="2026-09-26T00:00:00+00:00",
    )


GOOD_INSTRUCTION = "Explain how the twenty eighty treasury split is enforced in code."
GOOD_RESPONSE = (
    "The allocation happens in BudgetPolicy.allocate, which rounds the "
    "spendable share down and hands the remainder to the reserve, so the two "
    "always sum back to the original income without inventing a unit."
)


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "aegisai.db"
    connection = sqlite3.connect(str(path))
    connection.executescript(
        """
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    connection.commit()
    connection.close()
    return path


def add_message(
    database: Path,
    conversation_id: str,
    message_id: str,
    role: str,
    content: str,
    order: int,
) -> None:
    connection = sqlite3.connect(str(database))
    connection.execute(
        "INSERT OR IGNORE INTO conversations (id, created_at, updated_at) "
        "VALUES (?, ?, ?)",
        (conversation_id, "2026-09-26T00:00:00+00:00", "2026-09-26T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            message_id,
            conversation_id,
            role,
            content,
            f"2026-09-26T00:00:{order:02d}+00:00",
        ),
    )
    connection.commit()
    connection.close()


# -- secrets ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("key is AKIAIOSFODNN7EXAMPLE here", "aws_access_key"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "github_token"),
        ("use sk-proj-abcdefghijklmnopqrstuvwx", "openai_key"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private_key_block"),
        ("xoxb-1234567890-abcdefghij", "slack_token"),
        ("AIzaSyA1234567890abcdefghijklmnopqrstuv", "google_api_key"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwx", "bearer_token"),
        ('api_key = "supersecretvalue123"', "assigned_secret"),
    ],
)
def test_secret_patterns_are_detected(text: str, expected: str) -> None:
    assert expected in scan_secrets(text)


def test_clean_text_has_no_secrets() -> None:
    assert scan_secrets(GOOD_INSTRUCTION + GOOD_RESPONSE) == []


def test_secrets_are_redacted() -> None:
    redacted = redact_secrets("AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "[REDACTED]" in redacted


# -- pair extraction --------------------------------------------------------


def test_pairs_extracted_from_messages() -> None:
    rows = [
        ("c1", "m1", "user", GOOD_INSTRUCTION, "t1"),
        ("c1", "m2", "assistant", GOOD_RESPONSE, "t2"),
    ]
    pairs = extract_pairs(rows)
    assert len(pairs) == 1
    assert pairs[0].instruction == GOOD_INSTRUCTION
    assert pairs[0].response == GOOD_RESPONSE


def test_tool_messages_do_not_break_a_pair() -> None:
    rows = [
        ("c1", "m1", "user", GOOD_INSTRUCTION, "t1"),
        ("c1", "m2", "tool", '{"result": 42}', "t2"),
        ("c1", "m3", "assistant", GOOD_RESPONSE, "t3"),
    ]
    assert len(extract_pairs(rows)) == 1


def test_consecutive_users_keep_the_last_instruction() -> None:
    rows = [
        ("c1", "m1", "user", "first question about the reserve ratio here", "t1"),
        ("c1", "m2", "user", "second question about the reserve ratio here", "t2"),
        ("c1", "m3", "assistant", GOOD_RESPONSE, "t3"),
    ]
    pairs = extract_pairs(rows)
    assert len(pairs) == 1
    assert pairs[0].instruction.startswith("second")


def test_empty_assistant_reply_is_not_a_pair() -> None:
    rows = [
        ("c1", "m1", "user", GOOD_INSTRUCTION, "t1"),
        ("c1", "m2", "assistant", "   ", "t2"),
    ]
    assert extract_pairs(rows) == []


# -- filtering --------------------------------------------------------------


def test_good_pair_is_kept() -> None:
    stats = BuildStats()
    assert filter_pair(
        make_pair(GOOD_INSTRUCTION, GOOD_RESPONSE), QualityConfig(), stats
    )
    assert stats.accepted == 0
    assert stats.by_reason == {}


@pytest.mark.parametrize(
    "instruction,response,reason",
    [
        ("hi", GOOD_RESPONSE, "instruction_too_short"),
        (GOOD_INSTRUCTION, "too short", "response_too_short"),
        (GOOD_INSTRUCTION, "x" * 9000, "response_too_long"),
        (
            GOOD_INSTRUCTION,
            (
                "I cannot help with that request at all, sorry. The treasury "
                "module does not expose a path that would permit it."
            ),
            "hedging_response",
        ),
        (
            GOOD_INSTRUCTION,
            json.dumps(
                {
                    "tool": "calculator",
                    "arguments": {
                        "expression": "(2 + 3) * 4 - 10 / 5 + 7 ** 2",
                    },
                }
            ),
            "tool_protocol_dump",
        ),
        (
            "AKIAIOSFODNN7EXAMPLE and a long enough instruction here",
            GOOD_RESPONSE,
            "secret_detected",
        ),
    ],
)
def test_rejections_carry_reasons(instruction: str, response: str, reason: str) -> None:
    stats = BuildStats()
    assert filter_pair(make_pair(instruction, response), QualityConfig(), stats) is None
    assert reason in stats.by_reason


def test_secret_scan_runs_before_the_length_gate() -> None:
    """A short exchange with a live key must still be reported as a leak.

    The length filters are cheaper, so it is tempting to run them first. That
    would drop this pair as "too short" and the credential would never be
    surfaced, which is the one outcome that must not happen.
    """
    stats = BuildStats()
    result = filter_pair(
        make_pair("key?", "it is sk-proj-abcdefghijklmnopqrstuvwxyz0123 ok"),
        QualityConfig(),
        stats,
    )
    assert result is None
    assert "secret_detected" in stats.by_reason
    assert "instruction_too_short" not in stats.by_reason
    assert "response_too_short" not in stats.by_reason


def test_echoing_response_is_rejected() -> None:
    stats = BuildStats()
    echoed = GOOD_INSTRUCTION * 2
    assert (
        filter_pair(make_pair(GOOD_INSTRUCTION, echoed), QualityConfig(), stats) is None
    )
    assert "response_echoes_instruction" in stats.by_reason


def test_emails_are_redacted() -> None:
    stats = BuildStats()
    pair = make_pair(
        f"{GOOD_INSTRUCTION} Contact me at someone@example.com for details.",
        f"{GOOD_RESPONSE} Reach me at someone@example.com if that helps.",
    )
    cleaned = filter_pair(pair, QualityConfig(), stats)
    assert cleaned is not None
    assert "someone@example.com" not in cleaned.instruction
    assert "[email]" in cleaned.instruction


def test_hedging_can_be_allowed() -> None:
    stats = BuildStats()
    config = QualityConfig(allow_hedging=True)
    result = filter_pair(
        make_pair(
            GOOD_INSTRUCTION,
            "I cannot determine that from the code alone, so here is what the "
            "implementation actually does with the twenty eighty split.",
        ),
        config,
        stats,
    )
    assert result is not None


# -- deduplication ----------------------------------------------------------


def test_exact_duplicates_are_removed() -> None:
    stats = BuildStats()
    pairs = [
        make_pair(GOOD_INSTRUCTION, GOOD_RESPONSE, "a"),
        make_pair(GOOD_INSTRUCTION, GOOD_RESPONSE, "b"),
    ]
    assert len(deduplicate(pairs, QualityConfig(), stats)) == 1
    assert stats.duplicates == 1


def test_normalization_catches_accent_variants() -> None:
    assert normalize_for_dedup("café  au lait") == normalize_for_dedup("café au lait")


def test_near_duplicates_are_removed() -> None:
    stats = BuildStats()
    base = (
        "The treasury enforces the twenty eighty split by rounding the "
        "spendable share down and assigning the remainder to the reserve."
    )
    pairs = [
        make_pair(GOOD_INSTRUCTION, base, "a"),
        make_pair(GOOD_INSTRUCTION, base + " This keeps the total exact.", "b"),
    ]
    kept = deduplicate(pairs, QualityConfig(near_duplicate_threshold=0.6), stats)
    assert len(kept) == 1


def test_distinct_records_are_both_kept() -> None:
    stats = BuildStats()
    pairs = [
        make_pair(GOOD_INSTRUCTION, GOOD_RESPONSE, "a"),
        make_pair(
            "Describe how BM25 ranking works in this index implementation.",
            "BM25 combines inverse document frequency with saturated term "
            "frequency, scored in Python over postings fetched by term.",
            "b",
        ),
    ]
    assert len(deduplicate(pairs, QualityConfig(), stats)) == 2


# -- splitting --------------------------------------------------------------


def test_split_is_deterministic() -> None:
    records = [{"id": f"rec-{index}"} for index in range(200)]
    first = split_records(records)
    second = split_records(list(reversed(records)))
    # Compare membership, not order: the hash-based assignment is
    # order-independent, but the sequence within a bucket is not.
    for bucket in ("train", "validation", "test"):
        assert {record["id"] for record in first[bucket]} == {
            record["id"] for record in second[bucket]
        }


def test_split_covers_every_record_exactly_once() -> None:
    records = [{"id": f"rec-{index}"} for index in range(120)]
    buckets = split_records(records)
    seen = [record["id"] for items in buckets.values() for record in items]
    assert sorted(seen) == sorted(record["id"] for record in records)
    assert len(seen) == len(set(seen))


def test_split_ratios_must_be_sane() -> None:
    with pytest.raises(SelfImprovementError):
        split_records([], validation_ratio=0.7, test_ratio=0.7)
    with pytest.raises(SelfImprovementError):
        split_records([], validation_ratio=-0.1)


# -- end to end -------------------------------------------------------------


def test_build_writes_dataset_and_manifest(database: Path, tmp_path: Path) -> None:
    add_message(database, "c1", "m1", "user", GOOD_INSTRUCTION, 1)
    add_message(database, "c1", "m2", "assistant", GOOD_RESPONSE, 2)
    add_message(database, "c1", "m3", "user", "And what about the reserve ratio?", 3)
    add_message(database, "c1", "m4", "assistant", "x" * 9000, 4)

    output = tmp_path / "out.jsonl"
    manifest = tmp_path / "manifest.json"
    payload = build(database, output, manifest=manifest, dataset_id="test")

    lines = output.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    record = json.loads(lines[0])
    assert record["id"].startswith("test:")
    assert record["messages"][0] == {"role": "user", "content": GOOD_INSTRUCTION}
    assert record["metadata"]["source"] == "self_improve"

    written = json.loads(manifest.read_text(encoding="utf-8"))
    assert written["splits"]["train"] + written["splits"]["validation"] + written[
        "splits"
    ]["test"] == len(lines)
    assert "response_too_long" in written["stats"]["by_reason"]
    assert payload["stats"]["accepted"] == 1


def test_build_refuses_a_missing_database(tmp_path: Path) -> None:
    with pytest.raises(SelfImprovementError):
        build(tmp_path / "nope.db", tmp_path / "out.jsonl")


def test_build_on_an_empty_database(database: Path, tmp_path: Path) -> None:
    payload = build(database, tmp_path / "out.jsonl")
    assert payload["stats"]["accepted"] == 0
    assert payload["splits"]["train"] == 0


def test_max_records_is_respected(database: Path, tmp_path: Path) -> None:
    for index in range(12):
        add_message(
            database,
            "c1",
            f"m{index}a",
            "user",
            f"Question number {index} about the treasury reserve accounting model?",
            index * 2,
        )
        add_message(
            database,
            "c1",
            f"m{index}b",
            "assistant",
            f"Answer number {index} explaining the reserve accounting model in detail. "
            * 3,
            index * 2 + 1,
        )
    payload = build(database, tmp_path / "out.jsonl", max_records=5)
    assert payload["stats"]["accepted"] == 5
