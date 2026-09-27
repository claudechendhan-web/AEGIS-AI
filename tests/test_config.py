from pathlib import Path

import pytest

from core.config import Settings, load_settings
from core.errors import ConfigurationError


def test_settings_defaults_are_local_first() -> None:
    settings = Settings.from_environment({})

    assert settings.application_name == "AEGISAI"
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.inference_provider == "ollama"
    assert settings.model_name == "qwen2.5-coder:3b"
    assert settings.ollama_base_url == "http://127.0.0.1:11434"
    assert settings.database_path == Path("data/aegisai.db")


def test_environment_values_override_defaults() -> None:
    settings = Settings.from_environment(
        {
            "AEGISAI_APPLICATION_NAME": "Local AEGISAI",
            "AEGISAI_ENVIRONMENT": "test",
            "AEGISAI_LOG_LEVEL": "debug",
            "AEGISAI_INFERENCE_PROVIDER": "OLLAMA",
            "AEGISAI_MODEL_NAME": "llama3.2:latest",
            "AEGISAI_OLLAMA_BASE_URL": "http://localhost:11434/",
            "AEGISAI_DATABASE_PATH": "tmp/test.db",
        }
    )

    assert settings.application_name == "Local AEGISAI"
    assert settings.log_level == "DEBUG"
    assert settings.inference_provider == "ollama"
    assert settings.model_name == "llama3.2:latest"
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.database_path == Path("tmp/test.db")


def test_ollama_base_url_supports_standard_environment_alias() -> None:
    settings = Settings.from_environment({"OLLAMA_BASE_URL": "http://127.0.0.1:11435"})

    assert settings.ollama_base_url == "http://127.0.0.1:11435"


def test_load_settings_reads_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "settings.toml"
    config_path.write_text(
        """application_name = "Configured AEGISAI"
environment = "test"
log_level = "WARNING"
inference_provider = "ollama"
model_name = "configured-model"
ollama_base_url = "http://127.0.0.1:11436"
database_path = "var/aegisai.db"
""",
        encoding="utf-8",
    )

    settings = load_settings(config_path, environ={"AEGISAI_LOG_LEVEL": "ERROR"})

    assert settings.application_name == "Configured AEGISAI"
    assert settings.environment == "test"
    assert settings.log_level == "ERROR"
    assert settings.model_name == "configured-model"
    assert settings.ollama_base_url == "http://127.0.0.1:11436"
    assert settings.database_path == Path("var/aegisai.db")


def test_invalid_settings_are_rejected() -> None:
    with pytest.raises(ConfigurationError):
        Settings(ollama_base_url="not-a-url")
    with pytest.raises(ConfigurationError):
        Settings(log_level="TRACE")
    with pytest.raises(ConfigurationError):
        Settings(model_name=" ")


def test_unknown_configuration_key_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="unknown configuration keys"):
        Settings.from_mapping({"unknown": "value"}, environ={})
