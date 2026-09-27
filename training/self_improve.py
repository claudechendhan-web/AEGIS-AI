"""Turn the agent's own conversations into supervised fine-tuning data.

This is the honest version of "the model trains itself". It does not change
any weights. It mines the transcript store for exchanges worth learning from,
filters them hard, and writes a dataset you can fine-tune on later on a
machine that can actually run one.

The filtering is the whole point. Naively self-training on raw transcripts
produces a dataset dominated by the model's own failures, because failures are
the abundant case. So this module is deliberately aggressive, and every
rejection carries a machine-readable reason:

    {"reason": "secret_detected", "rule": "aws_access_key", "record": "..."}

A dataset you cannot audit is a dataset you cannot trust, and a fine-tune on
leaked API keys teaches the model to emit them.

Usage:

    python -m training.self_improve --database data/aegisai.db \\
        --output data/training/self_improve.jsonl --max-records 5000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_DATABASE = Path("data/aegisai.db")
DEFAULT_OUTPUT = Path("data/training/self_improve.jsonl")
DEFAULT_MANIFEST = Path("data/training/self_improve_manifest.json")

DEFAULT_MIN_INSTRUCTION_CHARS = 24
DEFAULT_MAX_INSTRUCTION_CHARS = 4000
DEFAULT_MIN_RESPONSE_CHARS = 80
DEFAULT_MAX_RESPONSE_CHARS = 8000

HEDGES = (
    "i cannot",
    "i can't",
    "i'm unable",
    "i am unable",
    "as an ai",
    "i don't know",
    "i do not know",
    "i'm not sure",
    "i am not sure",
)

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_key", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S{20,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_key", re.compile(r"\b[srp]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
    ("bearer_token", re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._-]{20,}")),
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[=:]\s*"
            r"['\"][^'\"\s]{12,}['\"]"
        ),
    ),
)

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
_WHITESPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9]+")


class SelfImprovementError(Exception):
    """Raised when the self-improvement pass cannot complete."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_for_dedup(text: str) -> str:
    """Fold text down to a comparable form.

    Unicodedata normalization matters more than it looks: the same answer
    typed on a keyboard with and without combining accents produces different
    bytes and would slip past an exact-hash dedup.
    """
    folded = unicodedata.normalize("NFKC", text).lower()
    return _WHITESPACE.sub(" ", folded).strip()


def content_key(instruction: str, response: str) -> str:
    payload = f"{normalize_for_dedup(instruction)}\x1f{normalize_for_dedup(response)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def shingles(text: str, size: int = 5) -> set[str]:
    """Word shingles, for near-duplicate detection."""
    words = _WORD.findall(normalize_for_dedup(text))
    if len(words) < size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    if not intersection:
        return 0.0
    return intersection / len(left | right)


@dataclass(frozen=True, slots=True)
class Rejection:
    """One record that did not make the dataset, and why."""

    record_id: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(slots=True)
class QualityConfig:
    min_instruction_chars: int = DEFAULT_MIN_INSTRUCTION_CHARS
    max_instruction_chars: int = DEFAULT_MAX_INSTRUCTION_CHARS
    min_response_chars: int = DEFAULT_MIN_RESPONSE_CHARS
    max_response_chars: int = DEFAULT_MAX_RESPONSE_CHARS
    near_duplicate_threshold: float = 0.85
    allow_hedging: bool = False
    strip_emails: bool = True
    require_tool_free: bool = True


@dataclass(slots=True)
class BuildStats:
    considered: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: list[Rejection] = field(default_factory=list)
    by_reason: dict[str, int] = field(default_factory=dict)

    def reject(self, record_id: str, reason: str, detail: str = "") -> None:
        self.rejected.append(Rejection(record_id, reason, detail))
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejected": len(self.rejected),
            "by_reason": dict(sorted(self.by_reason.items())),
        }


def scan_secrets(text: str) -> list[str]:
    """Return the names of every secret pattern present in `text`."""
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(text)]


def redact_secrets(text: str) -> str:
    """Replace anything that looks like a credential with a marker."""
    redacted = text
    for _, pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def strip_emails(text: str) -> str:
    return _EMAIL.sub("[email]", text)


def looks_like_tool_dump(text: str) -> bool:
    """True when the response is a raw protocol payload, not an answer."""
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return True


def echo_ratio(instruction: str, response: str) -> float:
    """How much of the response is just the question restated."""
    left = shingles(instruction)
    right = shingles(response)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left)


@dataclass(frozen=True, slots=True)
class Pair:
    record_id: str
    instruction: str
    response: str
    conversation_id: str
    created_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self, dataset_id: str) -> dict[str, Any]:
        return {
            "id": f"{dataset_id}:{self.record_id}",
            "messages": [
                {"role": "user", "content": self.instruction},
                {"role": "assistant", "content": self.response},
            ],
            "metadata": {
                "dataset_id": dataset_id,
                "source": "self_improve",
                "conversation_id": self.conversation_id,
                "created_at": self.created_at,
                **dict(self.metadata),
            },
        }


def iter_transcripts(
    database: str | Path, *, limit: int = 0
) -> Iterator[tuple[str, str, str, str, str]]:
    """Yield `(conversation_id, message_id, role, content, created_at)`.

    Reads the conversation tables directly rather than going through
    `storage`, so this keeps working against a database written by any
    version, and so a malformed row skips instead of aborting the run.
    """
    path = Path(database)
    if str(database) != ":memory:" and not path.exists():
        raise SelfImprovementError(f"database not found: {path}")
    try:
        connection = sqlite3.connect(str(database))
    except sqlite3.Error as exc:
        raise SelfImprovementError(f"unable to open {database}: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        query = (
            "SELECT conversation_id, id, role, content, created_at "
            "FROM messages ORDER BY conversation_id, created_at, id"
        )
        if limit > 0:
            query += f" LIMIT {int(limit)}"
        for row in connection.execute(query):
            yield (
                str(row["conversation_id"]),
                str(row["id"]),
                str(row["role"]),
                str(row["content"] or ""),
                str(row["created_at"]),
            )
    except sqlite3.Error as exc:
        raise SelfImprovementError(f"unable to read messages: {exc}") from exc
    finally:
        connection.close()


def extract_pairs(
    rows: Iterable[tuple[str, str, str, str, str]],
    *,
    max_turns_per_conversation: int = 40,
) -> list[Pair]:
    """Turn a message stream into user/assistant exchange pairs.

    A tool message between the user and the assistant does not break the
    pair: the agent's final answer is still the response to the user's
    question, and dropping those would throw away the most useful data the
    agent produces.
    """
    by_conversation: dict[str, list[tuple[str, str, str, str]]] = {}
    for conversation_id, message_id, role, content, created_at in rows:
        by_conversation.setdefault(conversation_id, []).append(
            (message_id, role, content, created_at)
        )

    pairs: list[Pair] = []
    for conversation_id, messages in by_conversation.items():
        pending: str | None = None
        pending_id = ""
        emitted = 0
        for message_id, role, content, created_at in messages:
            if role == "user":
                pending = content
                pending_id = message_id
            elif role == "assistant" and pending is not None:
                if content.strip():
                    pairs.append(
                        Pair(
                            record_id=pending_id,
                            instruction=pending,
                            response=content,
                            conversation_id=conversation_id,
                            created_at=created_at,
                        )
                    )
                    emitted += 1
                    if emitted >= max_turns_per_conversation:
                        break
                pending = None
    return pairs


def filter_pair(pair: Pair, config: QualityConfig, stats: BuildStats) -> Pair | None:
    """Apply every rule, returning a cleaned pair or None with a reason logged."""
    stats.considered += 1
    instruction = pair.instruction
    response = pair.response

    # The secret scan runs first, before any cheap length or shape filter.
    # Order matters here: a short exchange containing a live credential would
    # otherwise be dropped as "too short", and the leak would never be
    # reported. A credential in the transcript is worth surfacing whether or
    # not the surrounding exchange was ever usable.
    secrets = scan_secrets(f"{instruction}\n{response}")
    if secrets:
        stats.reject(pair.record_id, "secret_detected", ",".join(sorted(secrets)))
        return None

    if not instruction.strip():
        stats.reject(pair.record_id, "empty_instruction")
        return None
    if len(instruction) < config.min_instruction_chars:
        stats.reject(
            pair.record_id,
            "instruction_too_short",
            f"{len(instruction)} < {config.min_instruction_chars}",
        )
        return None
    if len(instruction) > config.max_instruction_chars:
        stats.reject(pair.record_id, "instruction_too_long")
        return None
    if len(response) < config.min_response_chars:
        stats.reject(
            pair.record_id,
            "response_too_short",
            f"{len(response)} < {config.min_response_chars}",
        )
        return None
    if len(response) > config.max_response_chars:
        stats.reject(pair.record_id, "response_too_long")
        return None

    if config.require_tool_free and looks_like_tool_dump(response):
        stats.reject(pair.record_id, "tool_protocol_dump")
        return None

    if not config.allow_hedging:
        lowered = response.lower()
        matched = [hedge for hedge in HEDGES if hedge in lowered]
        if matched:
            stats.reject(pair.record_id, "hedging_response", matched[0])
            return None

    if echo_ratio(instruction, response) > 0.7:
        stats.reject(pair.record_id, "response_echoes_instruction")
        return None

    cleaned_instruction = instruction
    cleaned_response = response
    if config.strip_emails:
        cleaned_instruction = strip_emails(cleaned_instruction)
        cleaned_response = strip_emails(cleaned_response)

    return Pair(
        record_id=pair.record_id,
        instruction=cleaned_instruction.strip(),
        response=cleaned_response.strip(),
        conversation_id=pair.conversation_id,
        created_at=pair.created_at,
        metadata=dict(pair.metadata),
    )


def deduplicate(
    pairs: Sequence[Pair], config: QualityConfig, stats: BuildStats
) -> list[Pair]:
    """Exact-hash dedup, then near-duplicate dedup on word shingles."""
    seen_keys: set[str] = set()
    kept: list[Pair] = []
    # Named `signatures`, not `shingles`: a local called `shingles` would
    # shadow the module-level shingles() function used on the next line.
    signatures: list[set[str]] = []
    for pair in pairs:
        key = content_key(pair.instruction, pair.response)
        if key in seen_keys:
            stats.duplicates += 1
            stats.reject(pair.record_id, "exact_duplicate")
            continue
        signature = shingles(pair.instruction + "\n" + pair.response)
        duplicate = False
        if config.near_duplicate_threshold < 1.0:
            for index, existing in enumerate(signatures):
                if jaccard(signature, existing) >= config.near_duplicate_threshold:
                    duplicate = True
                    stats.duplicates += 1
                    stats.reject(
                        pair.record_id,
                        "near_duplicate",
                        f"similar to {kept[index].record_id}",
                    )
                    break
        if duplicate:
            continue
        seen_keys.add(key)
        kept.append(pair)
        signatures.append(signature)
    return kept


def split_records(
    records: Sequence[Mapping[str, Any]],
    *,
    validation_ratio: float = 0.05,
    test_ratio: float = 0.05,
    seed: int = 20260926,
) -> dict[str, list[Mapping[str, Any]]]:
    """Deterministic train/validation/test split.

    Deterministic because a split that reshuffles on every run makes it
    impossible to tell whether a score changed because of the data or because
    of the shuffle.
    """
    if validation_ratio < 0 or test_ratio < 0:
        raise SelfImprovementError("split ratios must not be negative")
    total = validation_ratio + test_ratio
    if total >= 1.0:
        raise SelfImprovementError("validation and test ratios must sum to below 1.0")

    indexed = list(enumerate(records))
    buckets: dict[str, list[Mapping[str, Any]]] = {
        "train": [],
        "validation": [],
        "test": [],
    }
    for position, record in indexed:
        # A stable hash of the record id, so the split is reproducible and
        # independent of input ordering.
        digest = hashlib.sha256(
            f"{seed}:{record.get('id', position)}".encode()
        ).digest()
        bucket = digest[0] / 255.0
        if bucket < validation_ratio:
            buckets["validation"].append(record)
        elif bucket < validation_ratio + test_ratio:
            buckets["test"].append(record)
        else:
            buckets["train"].append(record)
    return buckets


def build(
    database: str | Path,
    output: str | Path,
    *,
    manifest: str | Path | None = None,
    dataset_id: str = "self_improve",
    max_records: int = 0,
    max_turns_per_conversation: int = 40,
    config: QualityConfig | None = None,
) -> dict[str, Any]:
    """Run the full pass and write the dataset plus a manifest."""
    settings = config or QualityConfig()
    stats = BuildStats()

    rows = list(iter_transcripts(database))
    candidates = extract_pairs(
        rows, max_turns_per_conversation=max_turns_per_conversation
    )
    cleaned: list[Pair] = []
    for pair in candidates:
        result = filter_pair(pair, settings, stats)
        if result is not None:
            cleaned.append(result)
    unique = deduplicate(cleaned, settings, stats)
    if max_records > 0:
        unique = unique[:max_records]
    stats.accepted = len(unique)

    records = [pair.to_record(dataset_id) for pair in unique]
    buckets = split_records(records)

    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("w", encoding="utf-8", newline="\n") as handle:
            for split in ("train", "validation", "test"):
                for record in buckets[split]:
                    handle.write(
                        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                    )
    except OSError as exc:
        raise SelfImprovementError(f"unable to write {destination}: {exc}") from exc

    manifest_path = Path(manifest) if manifest is not None else None
    payload = {
        "dataset_id": dataset_id,
        "generated_at": _now(),
        "source_database": str(database),
        "output": str(destination),
        "format": "messages",
        "fields": ["id", "messages", "metadata"],
        "splits": {name: len(items) for name, items in buckets.items()},
        "stats": stats.to_dict(),
        "rejections": [rejection.to_dict() for rejection in stats.rejected[:200]],
        "truncated": bool(stats.rejected[200:]),
        "config": {
            "min_instruction_chars": settings.min_instruction_chars,
            "max_instruction_chars": settings.max_instruction_chars,
            "min_response_chars": settings.min_response_chars,
            "max_response_chars": settings.max_response_chars,
            "near_duplicate_threshold": settings.near_duplicate_threshold,
            "require_tool_free": settings.require_tool_free,
            "allow_hedging": settings.allow_hedging,
        },
    }
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            manifest_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise SelfImprovementError(
                f"unable to write {manifest_path}: {exc}"
            ) from exc
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="training.self_improve",
        description=(
            "Build supervised fine-tuning data from the agent's own "
            "transcripts. Writes JSONL and a manifest; trains nothing."
        ),
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--dataset-id", default="self_improve")
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument(
        "--min-response-chars", type=int, default=DEFAULT_MIN_RESPONSE_CHARS
    )
    parser.add_argument("--near-duplicate-threshold", type=float, default=0.85)
    parser.add_argument(
        "--allow-hedging",
        action="store_true",
        help="keep responses that refuse or express uncertainty",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = QualityConfig(
        min_response_chars=args.min_response_chars,
        near_duplicate_threshold=args.near_duplicate_threshold,
        allow_hedging=args.allow_hedging,
    )
    try:
        payload = build(
            args.database,
            args.output,
            manifest=args.manifest,
            dataset_id=args.dataset_id,
            max_records=args.max_records,
            max_turns_per_conversation=args.max_turns,
            config=config,
        )
    except SelfImprovementError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload["stats"], indent=2))
    print(json.dumps(payload["splits"], indent=2))
    print(f"wrote {payload['output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
