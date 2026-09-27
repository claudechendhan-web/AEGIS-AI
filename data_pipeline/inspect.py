from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from data_pipeline.common import PipelineReport, write_json
from data_pipeline.config import PipelineConfig, PipelineError, SourceConfig


class HFInspectionClient:
    def __init__(
        self,
        *,
        timeout: float = 60.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.timeout = timeout
        self.opener = opener or urlopen

    def dataset_api(self, dataset_id: str) -> dict[str, Any]:
        url = f"https://huggingface.co/api/datasets/{quote(dataset_id, safe='/')}"
        return self._json(url)

    def dataset_server_info(self, dataset_id: str) -> dict[str, Any]:
        query = urlencode({"dataset": dataset_id})
        return self._json(f"https://datasets-server.huggingface.co/info?{query}")

    def first_rows(self, dataset_id: str, config: str, split: str) -> dict[str, Any]:
        query = urlencode({"dataset": dataset_id, "config": config, "split": split})
        return self._json(f"https://datasets-server.huggingface.co/first-rows?{query}")

    def remote_size(self, dataset_id: str, file_name: str) -> int | None:
        url = (
            f"https://huggingface.co/datasets/{quote(dataset_id, safe='/')}/"
            f"resolve/main/{quote(file_name, safe='/')}"
        )
        request = Request(url, method="HEAD")
        try:
            response = self.opener(request, timeout=self.timeout)
            try:
                value = response.headers.get("Content-Length")
                return int(value) if value is not None else None
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            return None

    def _json(self, url: str) -> dict[str, Any]:
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            response = self.opener(request, timeout=self.timeout)
            try:
                raw = response.read()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except HTTPError as exc:
            raise PipelineError(
                f"metadata request failed with HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise PipelineError(f"metadata request failed: {exc}") from exc
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PipelineError("metadata response is not valid JSON") from exc
        if not isinstance(value, dict):
            raise PipelineError("metadata response is not an object")
        return value


def inspect_source(
    name: str,
    source: SourceConfig,
    *,
    sample_limit: int = 3,
    client: Any = None,
) -> dict[str, Any]:
    if sample_limit <= 0:
        raise PipelineError("sample_limit must be greater than zero")
    client = client or HFInspectionClient()
    report = PipelineReport(stage="inspect", source=name)
    result: dict[str, Any] = {
        "name": name,
        "dataset_id": source.source_id,
        "format": source.dataset_format,
        "enabled": source.enabled,
        "recommended": source.recommended,
        "configured_files": list(source.files),
        "inspected_at": datetime.now(UTC).isoformat(),
    }
    try:
        metadata = client.dataset_api(source.source_id)
        result["revision"] = metadata.get("sha", "unknown")
        result["license"] = _license(metadata)
        result["private"] = bool(metadata.get("private", False))
        result["gated"] = bool(metadata.get("gated", False))
        result["used_storage_bytes"] = metadata.get("usedStorage")
        result["tags"] = metadata.get("tags", [])
        result["files"] = _file_inventory(source, client)
        result["configs"] = _configs(metadata)
        result["viewer"] = _viewer_inventory(source, client, sample_limit)
        report.increment("datasets_inspected")
    except PipelineError as exc:
        result["error"] = str(exc)
        report.errors.append(str(exc))
        report.increment("datasets_failed")
    result["report"] = report.finish().to_dict()
    return result


def inspect_all(
    config: PipelineConfig,
    output_path: str | Path,
    *,
    sample_limit: int = 3,
    client: HFInspectionClient | None = None,
) -> dict[str, Any]:
    inventories = [
        inspect_source(name, source, sample_limit=sample_limit, client=client)
        for name, source in config.sources.items()
    ]
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "config": config.to_dict(),
        "datasets": inventories,
        "summary": {
            "dataset_count": len(inventories),
            "failed_count": sum("error" in item for item in inventories),
            "viewer_error_count": sum(
                _has_viewer_error(item.get("viewer", {})) for item in inventories
            ),
        },
    }
    write_json(output_path, payload)
    return payload


def _license(metadata: Mapping[str, Any]) -> str:
    card_data = metadata.get("cardData")
    if isinstance(card_data, Mapping):
        value = card_data.get("license")
        if isinstance(value, str) and value.strip():
            return value
    return "unknown"


def _file_inventory(
    source: SourceConfig, client: HFInspectionClient
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for file_name in source.files:
        files.append(
            {
                "path": file_name,
                "size_bytes": client.remote_size(source.source_id, file_name),
            }
        )
    return files


def _configs(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    card_data = metadata.get("cardData")
    if not isinstance(card_data, Mapping):
        return []
    configs = card_data.get("configs")
    if not isinstance(configs, list):
        return []
    result: list[dict[str, Any]] = []
    for config in configs:
        if not isinstance(config, Mapping):
            continue
        result.append(
            {
                "name": config.get("config_name"),
                "data_files": config.get("data_files", []),
            }
        )
    return result


def _viewer_inventory(
    source: SourceConfig, client: Any, sample_limit: int
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "info_status": "not_checked",
        "sample_status": "not_checked",
    }
    try:
        info = client.dataset_server_info(source.source_id)
        result["info_status"] = "ok"
        result["info"] = info.get("dataset_info", {})
    except PipelineError as exc:
        result["info_status"] = "error"
        result["info_error"] = str(exc)
    if source.source_id == "neulab/agent-data-collection":
        result["sample_status"] = "skipped_collection_schema_error"
        return result
    if source.source_id == "nvidia/Nemotron-Agentic-v1":
        result["sample_status"] = "ok"
        result["samples"] = {}
        for split in ("interactive_agent", "tool_calling"):
            try:
                sample = client.first_rows(source.source_id, "default", split)
                result["samples"][split] = sample.get("rows", [])[:sample_limit]
            except PipelineError as exc:
                result["samples"][split] = {"error": str(exc)}
        return result
    try:
        sample = client.first_rows(source.source_id, "default", "train")
        result["sample_status"] = "ok"
        result["sample"] = sample.get("rows", [])[:sample_limit]
    except PipelineError as exc:
        result["sample_status"] = "error"
        result["sample_error"] = str(exc)
    return result


def _has_viewer_error(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("info_status") == "error" or value.get("sample_status") == "error":
        return True
    samples = value.get("samples")
    if isinstance(samples, Mapping):
        return any(
            isinstance(sample, Mapping) and "error" in sample
            for sample in samples.values()
        )
    return isinstance(samples, list) and any(
        isinstance(sample, Mapping) and "error" in sample for sample in samples
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect configured Hugging Face datasets"
    )
    parser.add_argument("--config", type=Path, default=Path("configs/dataset.toml"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("datasets/manifests/inventory.json"),
    )
    parser.add_argument("--sample-limit", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = PipelineConfig.load(args.config)
        config.ensure_directories()
        payload = inspect_all(config, args.output, sample_limit=args.sample_limit)
        print(json.dumps(payload["summary"], indent=2))
        return 0 if payload["summary"]["failed_count"] == 0 else 1
    except (PipelineError, OSError, ValueError) as exc:
        print(f"Inspection error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
