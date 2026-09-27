from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from data_pipeline.config import PipelineError


@dataclass(slots=True)
class SourceContext:
    source: str
    license_name: str
    revision: str = "unknown"
    file_name: str = ""
    format: str = "jsonl"
    extra: dict[str, Any] = field(default_factory=dict)

    def provenance(self, row_number: int) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_file": self.file_name,
            "row_number": row_number,
            "revision": self.revision,
            "license": self.license_name,
            "format": self.format,
            **self.extra,
        }


@dataclass(slots=True)
class PipelineReport:
    stage: str
    source: str = ""
    input_path: str = ""
    output_path: str = ""
    counters: dict[str, int] = field(default_factory=dict)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str = ""
    errors: list[str] = field(default_factory=list)

    def increment(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def finish(self) -> PipelineReport:
        self.finished_at = datetime.now(UTC).isoformat()
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "source": self.source,
            "input_path": self.input_path,
            "output_path": self.output_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "counters": dict(self.counters),
            "errors": list(self.errors),
        }


@dataclass(slots=True)
class Checkpoint:
    path: Path
    state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Checkpoint:
        checkpoint_path = Path(path)
        if not checkpoint_path.exists():
            return cls(checkpoint_path)
        try:
            state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineError(f"invalid checkpoint: {checkpoint_path}") from exc
        if not isinstance(state, dict):
            raise PipelineError(f"checkpoint must be an object: {checkpoint_path}")
        return cls(checkpoint_path, state)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(self.state, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _replace_path(temporary, self.path)

    def get_int(self, name: str, default: int = 0) -> int:
        value = self.state.get(name, default)
        return value if isinstance(value, int) else default


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def normalized_text_hash(value: str) -> str:
    return hashlib.sha256(normalized_text(value).encode("utf-8")).hexdigest()


def iter_jsonl(path: str | Path, start_line: int = 0) -> Iterator[tuple[int, str]]:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, start=1):
                if line_number <= start_line:
                    continue
                yield line_number, line
    except (OSError, UnicodeDecodeError) as exc:
        raise PipelineError(f"unable to read JSONL: {source}") from exc


def iter_input_records(
    path: str | Path,
    data_format: str = "jsonl",
    start_line: int = 0,
) -> Iterator[tuple[int, Any, str | None]]:
    source = Path(path)
    normalized_format = data_format.lower()
    if normalized_format == "parquet" or source.suffix.lower() == ".parquet":
        try:
            from pyarrow import parquet
        except ImportError as exc:
            raise PipelineError(
                "Parquet input requires the optional pyarrow package"
            ) from exc
        row_number = 0
        try:
            parquet_file = parquet.ParquetFile(source)
            for batch in parquet_file.iter_batches(batch_size=4096):
                for record in batch.to_pylist():
                    row_number += 1
                    if row_number > start_line:
                        yield row_number, record, None
        except (OSError, ValueError) as exc:
            raise PipelineError(f"unable to read Parquet: {source}") from exc
        return
    for line_number, line in iter_jsonl(source, start_line=start_line):
        try:
            yield line_number, json.loads(line), None
        except json.JSONDecodeError:
            yield line_number, None, "malformed JSON record"


def remaining_record_limit(max_records: int, start_line: int) -> int | None:
    if max_records == 0:
        return None
    return max(0, max_records - start_line)


def load_json_line(line: str) -> Any:
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise PipelineError("malformed JSON record") from exc


def _replace_path(source: Path, destination: Path) -> None:
    try:
        source.replace(destination)
    except PermissionError:
        destination.unlink(missing_ok=True)
        source.replace(destination)


def write_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _replace_path(temporary, destination)


def write_jsonl(path: str | Path, records: Iterable[Mapping[str, Any]]) -> int:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    count = 0
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(canonical_json(record))
            stream.write("\n")
            count += 1
    _replace_path(temporary, destination)
    return count


def write_report(path: str | Path, report: PipelineReport) -> None:
    write_json(path, report.finish().to_dict())


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PipelineError(f"unable to hash file: {path}") from exc
    return digest.hexdigest()


def file_size(path: str | Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError as exc:
        raise PipelineError(f"unable to stat file: {path}") from exc


def directory_size(path: str | Path) -> int:
    root = Path(path)
    if not root.exists():
        return 0
    if root.is_file():
        return file_size(root)
    total = 0
    try:
        for item in root.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError as exc:
        raise PipelineError(f"unable to measure directory: {root}") from exc
    return total


def relative_or_absolute(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def parse_nonnegative_int(value: Any, name: str, default: int = 0) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or value < 0:
        raise PipelineError(f"{name} must be a non-negative integer")
    return value


def parse_positive_int(value: Any, name: str, default: int = 1) -> int:
    parsed = parse_nonnegative_int(value, name, default)
    if parsed == 0:
        raise PipelineError(f"{name} must be greater than zero")
    return parsed
