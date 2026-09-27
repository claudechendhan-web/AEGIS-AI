import importlib
import json

from core.config import Settings
from core.errors import ProviderConnectionError
from core.models import AgentResponse
from inference.base import ProviderHealth

cli_main = importlib.import_module("cli.main")


class FakeRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.prompt: str | None = None

    def run(self, prompt: str) -> AgentResponse:
        self.prompt = prompt
        return AgentResponse(
            content="cli response",
            request_id="request-1",
            provider="fake",
            model="fake-model",
        )

    def health_check(self) -> ProviderHealth:
        return ProviderHealth("fake", "fake-model", True, "fake available", 1.0)


def test_cli_runs_prompt_and_prints_response(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_main, "Runtime", FakeRuntime)

    exit_code = cli_main.main(["Hello"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "cli response\n"
    assert "Hello" not in captured.err


def test_cli_health_check_prints_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_main, "Runtime", FakeRuntime)

    exit_code = cli_main.main(["--health"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["reachable"] is True
    assert payload["provider"] == "fake"


def test_cli_reports_provider_errors(monkeypatch, capsys) -> None:
    class FailingRuntime(FakeRuntime):
        def run(self, prompt: str) -> AgentResponse:
            raise ProviderConnectionError("offline")

    monkeypatch.setattr(cli_main, "Runtime", FailingRuntime)

    exit_code = cli_main.main(["Hello"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "offline" in captured.err
    assert captured.out == ""


def test_cli_requires_prompt_or_health(capsys) -> None:
    exit_code = cli_main.main([])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "prompt is required" in captured.err
