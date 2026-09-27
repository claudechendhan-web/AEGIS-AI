from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Iterable, Mapping
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from data_pipeline.common import (
    Checkpoint,
    PipelineReport,
    canonical_json,
    iter_jsonl,
    load_json_line,
    normalized_text,
    normalized_text_hash,
    remaining_record_limit,
    stable_hash,
    write_json,
)
from data_pipeline.config import PipelineConfig, PipelineError

InputPath = str | Path | Iterable[str | Path]


def deduplicate_jsonl(
    input_path: InputPath,
    output_path: str | Path,
    *,
    report_path: str | Path,
    duplicates_path: str | Path,
    near_duplicates: bool = False,
    near_threshold: float = 0.92,
    max_candidates: int = 1000,
    max_records: int = 0,
    resume: bool = True,
) -> PipelineReport:
    if max_records < 0:
        raise PipelineError("max_records must be zero or greater")
    if not 0.0 < near_threshold <= 1.0:
        raise PipelineError("near_threshold must be in (0, 1]")
    if max_candidates <= 0:
        raise PipelineError("max_candidates must be greater than zero")
    paths = _coerce_paths(input_path)
    if not paths:
        raise PipelineError("at least one input path is required")
    output = Path(output_path)
    duplicates = Path(duplicates_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    duplicates.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = Path(report_path).with_suffix(".checkpoint.json")
    state_path = Path(report_path).with_suffix(".sqlite")
    checkpoint = Checkpoint.load(checkpoint_path)
    if not resume:
        checkpoint.state = {}
        state_path.unlink(missing_ok=True)
    path_index = checkpoint.get_int("path_index")
    start_line = checkpoint.get_int("last_line")
    processed = checkpoint.get_int("records_seen")
    if len(paths) == 1 and "records_seen" not in checkpoint.state:
        processed = start_line
    path_index = min(path_index, len(paths))
    record_limit = remaining_record_limit(max_records, processed)
    connection = sqlite3.connect(state_path)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS exact_hashes (hash TEXT PRIMARY KEY)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS normalized_hashes (hash TEXT PRIMARY KEY)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS near_signatures (signature TEXT, hash TEXT, text TEXT)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_near_signatures ON near_signatures(signature)"
    )
    connection.commit()
    report = PipelineReport(
        stage="deduplicate",
        input_path=",".join(str(path) for path in paths),
        output_path=str(output),
    )
    stopped = False
    try:
        with (
            output.open(
                "a" if resume else "w", encoding="utf-8", newline="\n"
            ) as output_stream,
            duplicates.open(
                "a" if resume else "w", encoding="utf-8", newline="\n"
            ) as duplicate_stream,
        ):
            for index in range(path_index, len(paths)):
                if record_limit == 0:
                    break
                current_start = start_line if index == path_index else 0
                for line_number, line in iter_jsonl(
                    paths[index], start_line=current_start
                ):
                    if record_limit == 0:
                        stopped = True
                        break
                    report.increment("records_seen")
                    processed += 1
                    try:
                        record = load_json_line(line)
                    except PipelineError:
                        report.increment("malformed")
                        duplicate_stream.write(
                            canonical_json(
                                {
                                    "path": str(paths[index]),
                                    "row_number": line_number,
                                    "reason": "malformed JSON record",
                                }
                            )
                        )
                        duplicate_stream.write("\n")
                    else:
                        duplicate_reason = _process_record(
                            record,
                            connection,
                            output_stream,
                            duplicate_stream,
                            paths[index],
                            line_number,
                            near_duplicates,
                            near_threshold,
                            max_candidates,
                            report,
                        )
                        if duplicate_reason is None and isinstance(record, dict):
                            connection.commit()
                    output_stream.flush()
                    duplicate_stream.flush()
                    connection.commit()
                    checkpoint.state = {
                        "path_index": index,
                        "last_line": line_number,
                        "records_seen": processed,
                    }
                    checkpoint.save()
                    if (
                        record_limit is not None
                        and report.counters.get("records_seen", 0) >= record_limit
                    ):
                        stopped = True
                        break
                if stopped:
                    break
                checkpoint.state = {
                    "path_index": index + 1,
                    "last_line": 0,
                    "records_seen": processed,
                }
                checkpoint.save()
        connection.commit()
    finally:
        connection.close()
    report_payload = report.finish().to_dict()
    report_payload["paths"] = [str(path) for path in paths]
    report_payload["cumulative_records_seen"] = processed
    report_payload["near_duplicates"] = near_duplicates
    write_json(report_path, report_payload)
    return report


def _coerce_paths(input_path: InputPath) -> list[Path]:
    if isinstance(input_path, (str, Path)):
        return [Path(input_path)]
    return [Path(path) for path in input_path]


def _process_record(
    record: Any,
    connection: sqlite3.Connection,
    output_stream: Any,
    duplicate_stream: Any,
    path: Path,
    line_number: int,
    near_duplicates: bool,
    near_threshold: float,
    max_candidates: int,
    report: PipelineReport,
) -> str | None:
    if not isinstance(record, dict):
        report.increment("rejected")
        report.increment("not_object")
        duplicate_stream.write(
            canonical_json(
                {
                    "path": str(path),
                    "row_number": line_number,
                    "reason": "not_object",
                    "raw": record,
                }
            )
        )
        duplicate_stream.write("\n")
        return "not_object"
    record_hash = stable_hash(_record_content(record))
    text = _record_text(record)
    text_hash = normalized_text_hash(text)
    duplicate_reason = _find_duplicate(
        connection,
        record_hash,
        text_hash,
        text,
        near_duplicates,
        near_threshold,
        max_candidates,
    )
    if duplicate_reason is not None:
        report.increment("duplicates")
        report.increment(duplicate_reason)
        duplicate_stream.write(
            canonical_json(
                {
                    "path": str(path),
                    "row_number": line_number,
                    "reason": duplicate_reason,
                    "id": record.get("id"),
                }
            )
        )
        duplicate_stream.write("\n")
        return duplicate_reason
    output_stream.write(canonical_json(record))
    output_stream.write("\n")
    report.increment("accepted")
    _insert_fingerprints(
        connection,
        record_hash,
        text_hash,
        text,
        near_duplicates,
    )
    return None


def _find_duplicate(
    connection: sqlite3.Connection,
    record_hash: str,
    text_hash: str,
    text: str,
    near_duplicates: bool,
    near_threshold: float,
    max_candidates: int,
) -> str | None:
    if connection.execute(
        "SELECT 1 FROM exact_hashes WHERE hash = ?", (record_hash,)
    ).fetchone():
        return "exact_duplicate"
    if connection.execute(
        "SELECT 1 FROM normalized_hashes WHERE hash = ?", (text_hash,)
    ).fetchone():
        return "normalized_duplicate"
    if not near_duplicates:
        return None
    signatures = _signatures(text)
    if not signatures:
        return None
    placeholders = ",".join("?" for _ in signatures)
    rows = connection.execute(
        f"SELECT text FROM near_signatures WHERE signature IN ({placeholders}) LIMIT ?",
        (*signatures, max_candidates),
    ).fetchall()
    for row in rows:
        candidate = str(row[0])
        if SequenceMatcher(None, text, candidate).ratio() >= near_threshold:
            return "near_duplicate"
    return None


def _insert_fingerprints(
    connection: sqlite3.Connection,
    record_hash: str,
    text_hash: str,
    text: str,
    near_duplicates: bool,
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO exact_hashes(hash) VALUES (?)", (record_hash,)
    )
    connection.execute(
        "INSERT OR IGNORE INTO normalized_hashes(hash) VALUES (?)", (text_hash,)
    )
    if near_duplicates:
        connection.executemany(
            "INSERT INTO near_signatures(signature, hash, text) VALUES (?, ?, ?)",
            [
                (signature, record_hash, text[:100_000])
                for signature in _signatures(text)
            ],
        )


def _record_content(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "messages": record.get("messages", []),
        "tool_calls": record.get("tool_calls", []),
        "tool_results": record.get("tool_results", []),
    }


def _record_text(record: Mapping[str, Any]) -> str:
    values: list[str] = []
    messages = record.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, Mapping):
                values.append(str(message.get("content", "")))
                calls = message.get("tool_calls", [])
                if isinstance(calls, list):
                    values.extend(canonical_json(call) for call in calls)
    results = record.get("tool_results", [])
    if isinstance(results, list):
        values.extend(canonical_json(result) for result in results)
    return normalized_text(" ".join(values))


def _signatures(text: str) -> set[str]:
    words = re.findall(r"\w+", text)
    if len(words) < 5:
        return {text} if text else set()
    return {
        " ".join(words[index : index + 5]) for index in range(max(1, len(words) - 4))
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deduplicate normalized JSONL")
    parser.add_argument("--config", type=Path, default=Path("configs/dataset.toml"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--duplicates", type=Path, required=True)
    parser.add_argument("--near-duplicates", action="store_true")
    parser.add_argument("--near-threshold", type=float, default=0.92)
    parser.add_argument("--max-candidates", type=int, default=1000)
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument("--no-resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        PipelineConfig.load(args.config)
        report = deduplicate_jsonl(
            args.input,
            args.output,
            report_path=args.report,
            duplicates_path=args.duplicates,
            near_duplicates=args.near_duplicates,
            near_threshold=args.near_threshold,
            max_candidates=args.max_candidates,
            max_records=args.max_records,
            resume=not args.no_resume,
        )
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    except (PipelineError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"Deduplication error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
