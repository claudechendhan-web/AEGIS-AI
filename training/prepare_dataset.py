from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from core.errors import AegisError

DEFAULT_DATASET_ID = "OLMo-Coding/starcoder-python-instruct"
DEFAULT_MAX_RECORDS = 10_000
DEFAULT_MAX_FILES = 1
DEFAULT_MAX_INSTRUCTION_CHARS = 12_000
DEFAULT_MAX_CODE_CHARS = 48_000
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_OUTPUT_PATH = Path("data/training/starcoder_python_instruct.jsonl")
DEFAULT_MANIFEST_PATH = Path("data/training/manifest.json")


class DatasetPreparationError(AegisError):
    """Raised when a remote dataset cannot be safely prepared."""


@dataclass(frozen=True, slots=True)
class DatasetMetadata:
    dataset_id: str
    revision: str
    license_name: str
    file_names: tuple[str, ...]

    @classmethod
    def from_payload(
        cls, dataset_id: str, payload: Mapping[str, Any]
    ) -> DatasetMetadata:
        if not isinstance(payload, Mapping):
            raise DatasetPreparationError("dataset metadata is not an object")
        for field_name in ("private", "gated", "disabled"):
            if payload.get(field_name) is True:
                raise DatasetPreparationError(
                    f"dataset cannot be used because it is {field_name}"
                )
        card_data = payload.get("cardData")
        if not isinstance(card_data, Mapping):
            raise DatasetPreparationError("dataset metadata has no card data")
        license_name = card_data.get("license")
        if not isinstance(license_name, str) or not license_name.strip():
            raise DatasetPreparationError("dataset metadata has no declared license")
        siblings = payload.get("siblings")
        if not isinstance(siblings, list):
            raise DatasetPreparationError("dataset metadata has no file list")
        file_names: list[str] = []
        for sibling in siblings:
            if not isinstance(sibling, Mapping):
                continue
            file_name = sibling.get("rfilename")
            if isinstance(file_name, str) and file_name.endswith(".jsonl"):
                _validate_source_file(file_name)
                file_names.append(file_name)
        if not file_names:
            raise DatasetPreparationError("dataset contains no JSONL files")
        revision = payload.get("sha")
        if not isinstance(revision, str) or not revision.strip():
            revision = "unknown"
        return cls(
            dataset_id=dataset_id,
            revision=revision,
            license_name=license_name.strip(),
            file_names=tuple(sorted(set(file_names))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "revision": self.revision,
            "license": self.license_name,
            "file_names": list(self.file_names),
        }


@dataclass(slots=True)
class PreparationStats:
    dataset_id: str
    revision: str
    license_name: str
    source_files: list[str]
    output_path: Path
    manifest_path: Path
    downloaded_lines: int = 0
    accepted_records: int = 0
    skipped_records: int = 0
    output_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "revision": self.revision,
            "license": self.license_name,
            "source_files": list(self.source_files),
            "output_path": str(self.output_path),
            "manifest_path": str(self.manifest_path),
            "downloaded_lines": self.downloaded_lines,
            "accepted_records": self.accepted_records,
            "skipped_records": self.skipped_records,
            "output_bytes": self.output_bytes,
        }


class DatasetPreparer:
    def __init__(
        self,
        dataset_id: str = DEFAULT_DATASET_ID,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Callable[..., Any] | None = None,
        metadata_payload: Mapping[str, Any] | None = None,
    ) -> None:
        self.dataset_id = _validate_dataset_id(dataset_id)
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.timeout = timeout
        self._opener = opener or urlopen
        payload = (
            dict(metadata_payload)
            if metadata_payload is not None
            else self._fetch_json(self._metadata_url())
        )
        self.metadata = DatasetMetadata.from_payload(self.dataset_id, payload)

    def prepare(
        self,
        output_path: str | Path = DEFAULT_OUTPUT_PATH,
        manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_files: int = DEFAULT_MAX_FILES,
        source_files: list[str] | None = None,
        max_instruction_chars: int = DEFAULT_MAX_INSTRUCTION_CHARS,
        max_code_chars: int = DEFAULT_MAX_CODE_CHARS,
    ) -> PreparationStats:
        if max_records < 0:
            raise ValueError("max_records must be zero or greater")
        if max_files <= 0:
            raise ValueError("max_files must be greater than zero")
        if max_instruction_chars <= 0 or max_code_chars <= 0:
            raise ValueError("character limits must be greater than zero")
        selected_files = self._select_source_files(source_files, max_files)
        output = Path(output_path)
        manifest = Path(manifest_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = output.with_name(f".{output.name}.tmp")
        stats = PreparationStats(
            dataset_id=self.dataset_id,
            revision=self.metadata.revision,
            license_name=self.metadata.license_name,
            source_files=selected_files,
            output_path=output,
            manifest_path=manifest,
        )
        seen_ids: set[str] = set()
        try:
            with temporary_output.open(
                "w", encoding="utf-8", newline="\n"
            ) as output_file:
                for source_file in selected_files:
                    for line in self._iter_source_lines(source_file):
                        stats.downloaded_lines += 1
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise DatasetPreparationError(
                                f"invalid JSON in {source_file} at line {stats.downloaded_lines}"
                            ) from exc
                        normalized = self.normalize_record(
                            record,
                            source_file,
                            max_instruction_chars=max_instruction_chars,
                            max_code_chars=max_code_chars,
                        )
                        if normalized is None:
                            stats.skipped_records += 1
                            continue
                        output_id = str(normalized["id"])
                        if output_id in seen_ids:
                            stats.skipped_records += 1
                            continue
                        seen_ids.add(output_id)
                        output_file.write(
                            json.dumps(
                                normalized,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        )
                        output_file.write("\n")
                        stats.accepted_records += 1
                        if max_records and stats.accepted_records >= max_records:
                            break
                    if max_records and stats.accepted_records >= max_records:
                        break
            temporary_output.replace(output)
            stats.output_bytes = output.stat().st_size
            self._write_manifest(manifest, stats)
        except Exception:
            temporary_output.unlink(missing_ok=True)
            raise
        return stats

    def normalize_record(
        self,
        record: Any,
        source_file: str,
        *,
        max_instruction_chars: int = DEFAULT_MAX_INSTRUCTION_CHARS,
        max_code_chars: int = DEFAULT_MAX_CODE_CHARS,
    ) -> dict[str, Any] | None:
        if not isinstance(record, Mapping):
            return None
        instruction = record.get("instruction")
        text = record.get("text")
        original_id = record.get("id")
        if not isinstance(instruction, str) or not instruction.strip():
            return None
        if not isinstance(text, str) or not text.strip():
            return None
        if not isinstance(original_id, str) or not original_id.strip():
            return None
        if len(instruction) > max_instruction_chars:
            return None
        if len(text) > max_code_chars:
            return None
        source_metadata = record.get("metadata")
        if not isinstance(source_metadata, Mapping):
            source_metadata = {}
        metadata = {
            "dataset_id": self.dataset_id,
            "source_file": source_file,
            "original_id": original_id,
            "extension": source_metadata.get("extension"),
            "provenance": source_metadata.get("provenance"),
        }
        return {
            "id": f"{self.dataset_id}:{source_file}:{original_id}",
            "messages": [
                {"role": "user", "content": instruction},
                {"role": "assistant", "content": text},
            ],
            "metadata": metadata,
        }

    def _select_source_files(
        self, requested: list[str] | None, max_files: int
    ) -> list[str]:
        available = set(self.metadata.file_names)
        if requested:
            selected: list[str] = []
            for file_name in requested:
                _validate_source_file(file_name)
                if file_name not in available:
                    raise DatasetPreparationError(
                        f"source file is not in dataset metadata: {file_name}"
                    )
                if file_name not in selected:
                    selected.append(file_name)
            if len(selected) > max_files:
                raise DatasetPreparationError(
                    f"requested files exceed max_files={max_files}"
                )
            return selected
        candidates = [
            file_name
            for file_name in self.metadata.file_names
            if file_name.startswith("python3_")
        ]
        if not candidates:
            candidates = list(self.metadata.file_names)
        return sorted(candidates)[:max_files]

    def _metadata_url(self) -> str:
        return f"https://huggingface.co/api/datasets/{quote(self.dataset_id, safe='/')}"

    def _source_url(self, file_name: str) -> str:
        _validate_source_file(file_name)
        return (
            f"https://huggingface.co/datasets/{quote(self.dataset_id, safe='/')}/"
            f"resolve/main/{quote(file_name, safe='')}"
        )

    def _fetch_json(self, url: str) -> dict[str, Any]:
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        response = self._open(request)
        try:
            raw = response.read()
        finally:
            _close_response(response)
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DatasetPreparationError("dataset metadata is not UTF-8") from exc
        if not isinstance(raw, str):
            raise DatasetPreparationError("dataset metadata response is invalid")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DatasetPreparationError("dataset metadata is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise DatasetPreparationError("dataset metadata is not an object")
        return decoded

    def _iter_source_lines(self, file_name: str):
        request = Request(
            self._source_url(file_name),
            headers={"Accept": "application/jsonl"},
            method="GET",
        )
        response = self._open(request)
        try:
            while True:
                raw_line = response.readline()
                if not raw_line:
                    break
                if isinstance(raw_line, bytes):
                    try:
                        yield raw_line.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise DatasetPreparationError(
                            f"source file is not UTF-8: {file_name}"
                        ) from exc
                elif isinstance(raw_line, str):
                    yield raw_line
                else:
                    raise DatasetPreparationError(
                        f"source response is not line-oriented: {file_name}"
                    )
        finally:
            _close_response(response)

    def _open(self, request: Request) -> Any:
        try:
            response = self._opener(request, timeout=self.timeout)
        except HTTPError as exc:
            raise DatasetPreparationError(
                f"dataset request failed with HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DatasetPreparationError(f"dataset request failed: {exc}") from exc
        status = getattr(response, "status", 200)
        if isinstance(status, int) and status >= 400:
            _close_response(response)
            raise DatasetPreparationError(f"dataset request failed with HTTP {status}")
        return response

    def _write_manifest(self, path: Path, stats: PreparationStats) -> None:
        manifest = {
            "prepared_at": datetime.now(UTC).isoformat(),
            "format": "messages",
            "fields": ["id", "messages", "metadata"],
            **stats.to_dict(),
        }
        temporary_path = path.with_name(f".{path.name}.tmp")
        try:
            temporary_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(path)
        except OSError as exc:
            temporary_path.unlink(missing_ok=True)
            raise DatasetPreparationError("unable to write dataset manifest") from exc


def _validate_dataset_id(dataset_id: str) -> str:
    if not isinstance(dataset_id, str):
        raise DatasetPreparationError("dataset_id must be a string")
    parts = dataset_id.split("/")
    if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
        raise DatasetPreparationError("dataset_id must use the form owner/name")
    return dataset_id.strip()


def _validate_source_file(file_name: str) -> None:
    if (
        not isinstance(file_name, str)
        or not file_name
        or file_name.startswith("/")
        or "\\" in file_name
        or ".." in file_name.split("/")
    ):
        raise DatasetPreparationError(f"invalid dataset source file: {file_name}")


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m training.prepare_dataset",
        description="Prepare a bounded local SFT sample from a Hugging Face dataset.",
    )
    parser.add_argument("--dataset-id", default=DEFAULT_DATASET_ID)
    parser.add_argument(
        "--source-file",
        action="append",
        help="Dataset JSONL filename; repeat to select multiple files",
    )
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument(
        "--max-instruction-chars", type=int, default=DEFAULT_MAX_INSTRUCTION_CHARS
    )
    parser.add_argument("--max-code-chars", type=int, default=DEFAULT_MAX_CODE_CHARS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        preparer = DatasetPreparer(
            args.dataset_id,
            timeout=args.timeout,
        )
        stats = preparer.prepare(
            args.output,
            args.manifest,
            max_records=args.max_records,
            max_files=args.max_files,
            source_files=args.source_file,
            max_instruction_chars=args.max_instruction_chars,
            max_code_chars=args.max_code_chars,
        )
        print(json.dumps(stats.to_dict(), indent=2, ensure_ascii=False))
        return 0
    except (DatasetPreparationError, OSError, ValueError) as exc:
        print(f"Dataset preparation error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
