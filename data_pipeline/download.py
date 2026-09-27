from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from data_pipeline.common import (
    PipelineReport,
    _replace_path,
    canonical_json,
    file_size,
    relative_or_absolute,
    write_json,
)
from data_pipeline.config import PipelineConfig, PipelineError


@dataclass(frozen=True, slots=True)
class DownloadResult:
    source: str
    remote_path: str
    revision: str
    local_path: Path
    bytes_downloaded: int
    total_bytes: int
    sha256: str
    resumed: bool
    seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "remote_path": self.remote_path,
            "revision": self.revision,
            "local_path": str(self.local_path),
            "bytes_downloaded": self.bytes_downloaded,
            "total_bytes": self.total_bytes,
            "sha256": self.sha256,
            "resumed": self.resumed,
            "seconds": self.seconds,
        }


class ResumableDownloader:
    def __init__(
        self,
        *,
        timeout: float = 120.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if timeout <= 0:
            raise PipelineError("download timeout must be greater than zero")
        self.timeout = timeout
        self.opener = opener or urlopen

    def download(
        self,
        dataset_id: str,
        remote_path: str,
        local_path: str | Path,
        *,
        revision: str = "main",
        max_bytes: int | None = None,
        resume: bool = True,
    ) -> DownloadResult:
        _validate_remote_path(remote_path)
        if not revision or any(character in revision for character in "/\\"):
            raise PipelineError(f"invalid dataset revision: {revision}")
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f".{destination.name}.part")
        existing = partial.stat().st_size if resume and partial.exists() else 0
        if not resume and partial.exists():
            partial.unlink()
            existing = 0
        request = Request(self._url(dataset_id, remote_path, revision), method="GET")
        if existing:
            request.add_header("Range", f"bytes={existing}-")
        started = time.perf_counter()
        response = None
        downloaded = 0
        try:
            response = self._open(request)
            status = getattr(response, "status", 200)
            resumed = existing > 0 and status == 206
            if existing and not resumed:
                existing = 0
                partial.unlink(missing_ok=True)
            content_length = response.headers.get("Content-Length")
            response_size = int(content_length) if content_length is not None else None
            total_bytes = existing + response_size if response_size is not None else 0
            if max_bytes is not None and total_bytes > max_bytes:
                raise PipelineError(
                    f"remote file exceeds max_bytes={max_bytes}: {remote_path}"
                )
            mode = "ab" if existing else "wb"
            with partial.open(mode) as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if max_bytes is not None and existing + downloaded > max_bytes:
                        raise PipelineError(
                            f"remote file exceeds max_bytes={max_bytes}: {remote_path}"
                        )
                    stream.write(chunk)
        except HTTPError as exc:
            raise PipelineError(
                f"download failed with HTTP {exc.code}: {remote_path}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise PipelineError(f"download failed: {remote_path}: {exc}") from exc
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        if not partial.exists():
            raise PipelineError(f"download did not create a file: {remote_path}")
        _replace_path(partial, destination)
        digest = _sha256(destination)
        seconds = time.perf_counter() - started
        return DownloadResult(
            source=dataset_id,
            remote_path=remote_path,
            revision=revision,
            local_path=destination,
            bytes_downloaded=downloaded,
            total_bytes=file_size(destination),
            sha256=digest,
            resumed=existing > 0,
            seconds=seconds,
        )

    def _open(self, request: Request) -> Any:
        return self.opener(request, timeout=self.timeout)

    @staticmethod
    def _url(dataset_id: str, remote_path: str, revision: str = "main") -> str:
        return (
            f"https://huggingface.co/datasets/{quote(dataset_id, safe='/')}/"
            f"resolve/{quote(revision, safe='')}/{quote(remote_path, safe='/')}"
        )


def download_sources(
    config: PipelineConfig,
    output_root: str | Path,
    *,
    source_names: list[str] | None = None,
    max_bytes: int | None = None,
    resume: bool = True,
    downloader: ResumableDownloader | None = None,
) -> dict[str, Any]:
    config.ensure_directories()
    downloader = downloader or ResumableDownloader()
    if source_names is None:
        raise PipelineError(
            "explicit source selection is required; refusing to download all datasets"
        )
    names = source_names
    output = Path(output_root)
    report = PipelineReport(stage="download", output_path=relative_or_absolute(output))
    manifest_path = config.manifests / "downloads.jsonl"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for name in names:
        source = config.source(name)
        if not source.enabled:
            report.increment("files_failed")
            report.errors.append(f"{name}: source is disabled by configuration")
            continue
        revision = _observed_revision(config, source.source_id)
        for remote_path in source.files:
            local_path = output / name / remote_path
            try:
                result = downloader.download(
                    source.source_id,
                    remote_path,
                    local_path,
                    revision=revision,
                    max_bytes=min(
                        value
                        for value in (source.max_bytes, max_bytes)
                        if value is not None
                    )
                    if any(value is not None for value in (source.max_bytes, max_bytes))
                    else None,
                    resume=resume,
                )
                results.append(result.to_dict())
                report.increment("files_completed")
                report.increment("bytes_downloaded", result.bytes_downloaded)
                with manifest_path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(canonical_json(result.to_dict()))
                    stream.write("\n")
            except PipelineError as exc:
                report.increment("files_failed")
                report.errors.append(f"{name}:{remote_path}: {exc}")
    report_payload = report.finish().to_dict()
    write_json(config.manifests / "download-report.json", report_payload)
    return {"results": results, "report": report_payload}


def _observed_revision(config: PipelineConfig, source_id: str) -> str:
    inventory_path = config.manifests / "inventory.json"
    if not inventory_path.exists():
        return "main"
    try:
        payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "main"
    datasets = payload.get("datasets", []) if isinstance(payload, dict) else []
    if not isinstance(datasets, list):
        return "main"
    for item in datasets:
        if not isinstance(item, dict) or item.get("dataset_id") != source_id:
            continue
        revision = item.get("revision")
        if isinstance(revision, str) and revision.strip():
            return revision
    return "main"


def _validate_remote_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or "\\" in path
        or ".." in path.split("/")
    ):
        raise PipelineError(f"invalid remote path: {path}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download bounded raw dataset files")
    parser.add_argument("--config", type=Path, default=Path("configs/dataset.toml"))
    parser.add_argument("--output", type=Path, default=Path("datasets/raw"))
    parser.add_argument("--source", action="append")
    parser.add_argument("--max-bytes", type=int)
    parser.add_argument("--no-resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = PipelineConfig.load(args.config)
        payload = download_sources(
            config,
            args.output,
            source_names=args.source,
            max_bytes=args.max_bytes,
            resume=not args.no_resume,
        )
        print(json.dumps(payload["report"], indent=2))
        return 0 if not payload["report"]["errors"] else 1
    except (PipelineError, OSError, ValueError) as exc:
        print(f"Download error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
