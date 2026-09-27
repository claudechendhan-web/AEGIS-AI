import importlib
import json
from pathlib import Path

from core.config import Settings
from core.models import AgentRequest, AgentResponse
from inference.base import InferenceProvider, ProviderHealth
from runtime.runtime import Runtime
from storage.sqlite import SQLiteStorage

cli_main = importlib.import_module("cli.main")


class CliProvider(InferenceProvider):
    @property
    def name(self) -> str:
        return "cli-test"

    def generate(self, request: AgentRequest) -> AgentResponse:
        assert request.conversation is not None
        return AgentResponse(
            content=f"response:{len(request.conversation.messages)}",
            request_id=request.request_id,
            provider=self.name,
            model="cli-test-model",
            conversation=request.conversation,
        )

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(self.name, "cli-test-model", True, "available")


def make_runtime(settings: Settings) -> Runtime:
    return Runtime(
        settings,
        provider=CliProvider(),
        storage=SQLiteStorage(settings.database_path),
    )


def test_cli_creates_and_continues_conversation(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    database_path = tmp_path / "cli.db"
    monkeypatch.setattr(cli_main, "Runtime", make_runtime)

    assert cli_main.main(["chat", "--new", "--database-path", str(database_path)]) == 0
    created_output = capsys.readouterr().out.strip()
    conversation_id = created_output.removeprefix("Conversation ID: ")

    assert (
        cli_main.main(
            [
                "chat",
                "--id",
                conversation_id,
                "Remember that the answer is blue.",
                "--database-path",
                str(database_path),
            ]
        )
        == 0
    )
    assert "response:1" in capsys.readouterr().out

    assert (
        cli_main.main(
            [
                "chat",
                "--id",
                conversation_id,
                "What is the answer?",
                "--database-path",
                str(database_path),
            ]
        )
        == 0
    )
    assert "response:3" in capsys.readouterr().out

    assert (
        cli_main.main(
            ["conversations", "--json", "--database-path", str(database_path)]
        )
        == 0
    )
    conversations = json.loads(capsys.readouterr().out)
    assert len(conversations) == 1
    assert conversations[0]["conversation_id"] == conversation_id
    assert len(conversations[0]["messages"]) == 4

    assert (
        cli_main.main(
            ["conversation", conversation_id, "--database-path", str(database_path)]
        )
        == 0
    )
    shown = json.loads(capsys.readouterr().out)
    assert shown["conversation_id"] == conversation_id
    assert shown["messages"][0]["content"] == "Remember that the answer is blue."


def test_cli_accepts_global_options_before_subcommand(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    database_path = tmp_path / "before.db"
    monkeypatch.setattr(cli_main, "Runtime", make_runtime)

    assert cli_main.main(["--database-path", str(database_path), "chat", "--new"]) == 0
    assert capsys.readouterr().out.startswith("Conversation ID: ")


def test_cli_calculator_outputs_structured_result(capsys) -> None:
    assert cli_main.main(["tool", "calculator", "--expression", "2 + 3 * 4"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["success"] is True
    assert result["output"] == {"result": 14}


def test_cli_reports_unknown_conversation_without_traceback(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    database_path = tmp_path / "cli.db"
    monkeypatch.setattr(cli_main, "Runtime", make_runtime)

    exit_code = cli_main.main(
        ["conversation", "missing", "--database-path", str(database_path)]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "conversation not found" in captured.err
    assert "Traceback" not in captured.err


def test_cli_health_command_uses_runtime(monkeypatch, capsys) -> None:
    class HealthyRuntime:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        def health_check(self) -> ProviderHealth:
            return ProviderHealth("healthy", "model", True, "available")

        def close(self) -> None:
            return None

    monkeypatch.setattr(cli_main, "Runtime", HealthyRuntime)

    assert cli_main.main(["health"]) == 0
    assert json.loads(capsys.readouterr().out)["reachable"] is True
