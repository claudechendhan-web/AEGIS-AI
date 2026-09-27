import json
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request

import pytest

from training.prepare_dataset import (
    DatasetMetadata,
    DatasetPreparationError,
    DatasetPreparer,
)


class FakeResponse:
    def __init__(self, lines: list[dict[str, Any]]) -> None:
        self.status = 200
        self._lines = [json.dumps(line).encode("utf-8") + b"\n" for line in lines]
        self._index = 0
        self.closed = False

    def read(self) -> bytes:
        return b"".join(self._lines)

    def readline(self) -> bytes:
        if self._index >= len(self._lines):
            return b""
        line = self._lines[self._index]
        self._index += 1
        return line

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, lines: list[dict[str, Any]]) -> None:
        self.lines = lines
        self.requests: list[tuple[str, float]] = []

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        full_url = request.full_url
        self.requests.append((full_url, timeout))
        return FakeResponse(self.lines)


def metadata_payload() -> dict[str, Any]:
    return {
        "private": False,
        "gated": False,
        "disabled": False,
        "sha": "revision-1",
        "cardData": {"license": "apache-2.0"},
        "siblings": [
            {"rfilename": "python2_chunk_instruct_00.jsonl"},
            {"rfilename": "python3_chunk_instruct_00.jsonl"},
            {"rfilename": "README.md"},
        ],
    }


def record(identifier: str, instruction: str = "Write code", text: str = "print(1)"):
    return {
        "instruction": instruction,
        "text": text,
        "id": identifier,
        "metadata": {
            "extension": "python3",
            "provenance": "source:1",
        },
    }


def make_preparer(lines: list[dict[str, Any]]) -> tuple[DatasetPreparer, FakeOpener]:
    opener = FakeOpener(lines)
    preparer = DatasetPreparer(
        opener=opener,
        metadata_payload=metadata_payload(),
        timeout=4.0,
    )
    return preparer, opener


def test_prepare_writes_bounded_sft_messages_and_manifest(tmp_path: Path) -> None:
    preparer, opener = make_preparer([record("1"), record("2"), record("3")])
    output = tmp_path / "sample.jsonl"
    manifest = tmp_path / "manifest.json"

    stats = preparer.prepare(
        output,
        manifest,
        max_records=2,
        source_files=["python3_chunk_instruct_00.jsonl"],
    )

    assert stats.accepted_records == 2
    assert stats.downloaded_lines == 2
    assert stats.license_name == "apache-2.0"
    assert stats.source_files == ["python3_chunk_instruct_00.jsonl"]
    assert opener.requests[0][0].endswith(
        "/resolve/main/python3_chunk_instruct_00.jsonl"
    )
    records = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 2
    assert records[0]["messages"] == [
        {"role": "user", "content": "Write code"},
        {"role": "assistant", "content": "print(1)"},
    ]
    saved_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved_manifest["dataset_id"] == "OLMo-Coding/starcoder-python-instruct"
    assert saved_manifest["format"] == "messages"
    assert saved_manifest["accepted_records"] == 2


def test_prepare_skips_invalid_duplicate_and_oversized_records(tmp_path: Path) -> None:
    preparer, _ = make_preparer(
        [
            record("same"),
            {"instruction": "missing text", "id": "missing"},
            record("same"),
            record("long", text="x" * 20),
            record("valid"),
        ]
    )

    stats = preparer.prepare(
        tmp_path / "sample.jsonl",
        tmp_path / "manifest.json",
        max_records=10,
        source_files=["python3_chunk_instruct_00.jsonl"],
        max_code_chars=10,
    )

    assert stats.accepted_records == 2
    assert stats.skipped_records == 3
    output = [
        json.loads(line)
        for line in (tmp_path / "sample.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(output) == 2
    assert output[1]["metadata"]["original_id"] == "valid"


def test_default_selection_prefers_python3_source(tmp_path: Path) -> None:
    preparer, opener = make_preparer([record("1")])

    preparer.prepare(
        output_path=tmp_path / "test-default.jsonl",
        manifest_path=tmp_path / "test-default-manifest.json",
        max_records=1,
        max_files=1,
    )

    assert "python3_chunk_instruct_00.jsonl" in opener.requests[0][0]


def test_metadata_rejects_gated_or_unlicensed_dataset() -> None:
    private_payload = metadata_payload()
    private_payload["gated"] = True
    with pytest.raises(DatasetPreparationError, match="gated"):
        DatasetMetadata.from_payload("owner/name", private_payload)

    unlicensed_payload = metadata_payload()
    unlicensed_payload["cardData"] = {}
    with pytest.raises(DatasetPreparationError, match="license"):
        DatasetMetadata.from_payload("owner/name", unlicensed_payload)


def test_invalid_source_file_is_rejected(tmp_path: Path) -> None:
    preparer, _ = make_preparer([record("1")])

    with pytest.raises(DatasetPreparationError, match="invalid dataset source file"):
        preparer.prepare(
            tmp_path / "sample.jsonl",
            tmp_path / "manifest.json",
            source_files=["../secret.jsonl"],
        )


def test_network_errors_are_wrapped(tmp_path: Path) -> None:
    def failing_opener(request: object, *, timeout: float) -> FakeResponse:
        raise URLError("offline")

    with pytest.raises(DatasetPreparationError, match="offline"):
        DatasetPreparer(
            opener=failing_opener,
            metadata_payload=metadata_payload(),
        ).prepare(
            tmp_path / "sample.jsonl",
            tmp_path / "manifest.json",
            source_files=["python3_chunk_instruct_00.jsonl"],
        )
