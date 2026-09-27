from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.errors import AegisError


class PipelineError(AegisError):
    """Raised when a data-pipeline operation cannot be completed."""


@dataclass(frozen=True, slots=True)
class SourceConfig:
    source_id: str
    dataset_format: str
    license_name: str
    files: tuple[str, ...] = ()
    enabled: bool = True
    recommended: bool = False
    max_bytes: int | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "format": self.dataset_format,
            "license": self.license_name,
            "files": list(self.files),
            "enabled": self.enabled,
            "recommended": self.recommended,
            "max_bytes": self.max_bytes,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    config_path: Path
    root: Path
    raw: Path
    staging: Path
    processed: Path
    manifests: Path
    sources: dict[str, SourceConfig]
    mixture: dict[str, float]
    limits: dict[str, Any]
    quality: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> PipelineConfig:
        config_path = Path(path).expanduser()
        try:
            payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise PipelineError(
                f"unable to load pipeline config: {config_path}"
            ) from exc
        if not isinstance(payload, dict):
            raise PipelineError("pipeline config must be a TOML object")
        paths = payload.get("paths", {})
        if not isinstance(paths, dict):
            raise PipelineError("pipeline paths must be a table")
        base = (
            config_path.parent.parent
            if config_path.parent.name == "configs"
            else config_path.parent
        )
        root = Path(str(paths.get("root", "datasets")))
        if not root.is_absolute():
            root = base / root

        def resolve_path(value: Any, default: str) -> Path:
            selected = Path(str(value or default))
            return selected if selected.is_absolute() else root / selected

        raw = resolve_path(paths.get("raw"), "raw")
        staging = resolve_path(paths.get("staging"), "staging")
        processed = resolve_path(paths.get("processed"), "processed")
        manifests = resolve_path(paths.get("manifests"), "manifests")
        source_values = payload.get("sources", {})
        if not isinstance(source_values, dict):
            raise PipelineError("pipeline sources must be a table")
        sources: dict[str, SourceConfig] = {}
        for name, value in source_values.items():
            if not isinstance(value, dict):
                raise PipelineError(f"source configuration is invalid: {name}")
            files_value = value.get("files", [])
            if not isinstance(files_value, list) or not all(
                isinstance(item, str) for item in files_value
            ):
                raise PipelineError(f"source files must be a list: {name}")
            max_bytes = value.get("max_bytes")
            if max_bytes is not None and (
                not isinstance(max_bytes, int) or max_bytes <= 0
            ):
                raise PipelineError(f"source max_bytes is invalid: {name}")
            sources[name] = SourceConfig(
                source_id=str(value.get("id", name)),
                dataset_format=str(value.get("format", "jsonl")),
                license_name=str(value.get("license", "unknown")),
                files=tuple(files_value),
                enabled=bool(value.get("enabled", True)),
                recommended=bool(value.get("recommended", False)),
                max_bytes=max_bytes,
                notes=str(value.get("notes", "")),
            )
        mixture_value = payload.get("mixture", {})
        if not isinstance(mixture_value, dict):
            raise PipelineError("pipeline mixture must be a table")
        mixture: dict[str, float] = {}
        for name, value in mixture_value.items():
            if not isinstance(value, (int, float)) or value < 0:
                raise PipelineError(f"mixture weight is invalid: {name}")
            mixture[str(name)] = float(value)
        if not mixture or not any(weight > 0 for weight in mixture.values()):
            raise PipelineError("pipeline mixture must contain a positive weight")
        limits = payload.get("limits", {})
        quality = payload.get("quality", {})
        if not isinstance(limits, dict) or not isinstance(quality, dict):
            raise PipelineError("pipeline limits and quality must be tables")
        return cls(
            config_path=config_path,
            root=root,
            raw=raw,
            staging=staging,
            processed=processed,
            manifests=manifests,
            sources=sources,
            mixture=mixture,
            limits=dict(limits),
            quality=dict(quality),
        )

    def source(self, name: str) -> SourceConfig:
        try:
            return self.sources[name]
        except KeyError as exc:
            raise PipelineError(f"unknown pipeline source: {name}") from exc

    def ensure_directories(self) -> None:
        for path in (self.raw, self.staging, self.processed, self.manifests):
            path.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "root": str(self.root),
            "paths": {
                "raw": str(self.raw),
                "staging": str(self.staging),
                "processed": str(self.processed),
                "manifests": str(self.manifests),
            },
            "sources": {
                name: source.to_dict() for name, source in self.sources.items()
            },
            "mixture": dict(self.mixture),
            "limits": dict(self.limits),
            "quality": dict(self.quality),
        }
