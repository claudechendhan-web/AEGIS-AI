import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.errors import ConfigurationError

DEFAULT_APPLICATION_NAME = "AEGISAI"
DEFAULT_ENVIRONMENT = "development"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_INFERENCE_PROVIDER = "ollama"
DEFAULT_MODEL_NAME = "qwen2.5-coder:3b"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_DATABASE_PATH = "data/aegisai.db"

_FIELD_NAMES = (
    "application_name",
    "environment",
    "log_level",
    "inference_provider",
    "model_name",
    "ollama_base_url",
    "database_path",
)
_ENV_NAMES = {
    "application_name": "AEGISAI_APPLICATION_NAME",
    "environment": "AEGISAI_ENVIRONMENT",
    "log_level": "AEGISAI_LOG_LEVEL",
    "inference_provider": "AEGISAI_INFERENCE_PROVIDER",
    "model_name": "AEGISAI_MODEL_NAME",
    "ollama_base_url": "AEGISAI_OLLAMA_BASE_URL",
    "database_path": "AEGISAI_DATABASE_PATH",
}
_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}


@dataclass(frozen=True, slots=True)
class Settings:
    application_name: str = DEFAULT_APPLICATION_NAME
    environment: str = DEFAULT_ENVIRONMENT
    log_level: str = DEFAULT_LOG_LEVEL
    inference_provider: str = DEFAULT_INFERENCE_PROVIDER
    model_name: str = DEFAULT_MODEL_NAME
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    database_path: Path = Path(DEFAULT_DATABASE_PATH)

    def __post_init__(self) -> None:
        application_name = self._required_string(
            self.application_name, "application_name"
        )
        environment = self._required_string(self.environment, "environment")
        log_level = self._required_string(self.log_level, "log_level").upper()
        inference_provider = self._required_string(
            self.inference_provider, "inference_provider"
        ).lower()
        model_name = self._required_string(self.model_name, "model_name")
        ollama_base_url = self._required_string(self.ollama_base_url, "ollama_base_url")
        ollama_base_url = ollama_base_url.rstrip("/")
        parsed_url = urlparse(ollama_base_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ConfigurationError(
                "ollama_base_url must be an absolute HTTP or HTTPS URL without query or fragment"
            )
        if log_level not in _LOG_LEVELS:
            allowed = ", ".join(sorted(_LOG_LEVELS))
            raise ConfigurationError(f"log_level must be one of: {allowed}")
        if not isinstance(self.database_path, (str, Path)):
            raise ConfigurationError("database_path must be a path")
        database_path = Path(self.database_path).expanduser()
        if not str(database_path).strip():
            raise ConfigurationError("database_path must not be empty")
        object.__setattr__(self, "application_name", application_name)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "log_level", log_level)
        object.__setattr__(self, "inference_provider", inference_provider)
        object.__setattr__(self, "model_name", model_name)
        object.__setattr__(self, "ollama_base_url", ollama_base_url)
        object.__setattr__(self, "database_path", database_path)

    @staticmethod
    def _required_string(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError(f"{name} must be a non-empty string")
        return value.strip()

    def to_dict(self) -> dict[str, str]:
        return {
            "application_name": self.application_name,
            "environment": self.environment,
            "log_level": self.log_level,
            "inference_provider": self.inference_provider,
            "model_name": self.model_name,
            "ollama_base_url": self.ollama_base_url,
            "database_path": str(self.database_path),
        }

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "Settings":
        supplied = dict(values or {})
        unknown = sorted(set(supplied) - set(_FIELD_NAMES))
        if unknown:
            names = ", ".join(unknown)
            raise ConfigurationError(f"unknown configuration keys: {names}")
        environment = os.environ if environ is None else environ
        kwargs: dict[str, Any] = {}
        for field_name in _FIELD_NAMES:
            env_name = _ENV_NAMES[field_name]
            if env_name in environment:
                kwargs[field_name] = environment[env_name]
            elif field_name == "ollama_base_url" and "OLLAMA_BASE_URL" in environment:
                kwargs[field_name] = environment["OLLAMA_BASE_URL"]
            elif field_name in supplied:
                kwargs[field_name] = supplied[field_name]
        return cls(**kwargs)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        return cls.from_mapping(environ=environ)


def load_settings(
    config_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    values: dict[str, Any] = {}
    if config_path is not None:
        path = Path(config_path).expanduser()
        try:
            raw_config = path.read_bytes()
        except OSError as exc:
            raise ConfigurationError(
                f"unable to read configuration file: {path}"
            ) from exc
        try:
            parsed_config = tomllib.loads(raw_config.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"invalid configuration file: {path}") from exc
        values.update(parsed_config)
    return Settings.from_mapping(values, environ=environ)
