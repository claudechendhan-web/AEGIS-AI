from pathlib import Path

import pytest

from core.config import Settings
from core.errors import ProviderConnectionError
from core.models import AgentRequest, AgentResponse
from inference.base import InferenceProvider, ProviderHealth
from runtime.runtime import Runtime
from storage.sqlite import SQLiteStorage


class HistoryProvider(InferenceProvider):
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []
        self.received_histories: list[list[str]] = []

    @property
    def name(self) -> str:
        return "history-test"

    def generate(self, request: AgentRequest) -> AgentResponse:
        self.requests.append(request)
        assert request.conversation is not None
        history = [message.content for message in request.conversation.messages]
        self.received_histories.append(history)
        return AgentResponse(
            content=f"reply-{len(history)}",
            request_id=request.request_id,
            provider=self.name,
            model="history-model",
            conversation=request.conversation,
        )

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(self.name, "history-model", True, "available")


class FailingProvider(HistoryProvider):
    def generate(self, request: AgentRequest) -> AgentResponse:
        raise ProviderConnectionError("history provider offline")


def test_runtime_persists_conversation_and_loads_history(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.db"
    storage = SQLiteStorage(database_path)
    provider = HistoryProvider()
    runtime = Runtime(
        Settings(database_path=database_path), provider=provider, storage=storage
    )

    first = runtime.run("My favorite color is blue.")
    assert first.conversation is not None
    conversation_id = first.conversation.id
    second = runtime.run("What is my favorite color?", conversation_id=conversation_id)

    assert second.conversation is not None
    assert second.conversation.id == conversation_id
    assert len(provider.requests) == 2
    assert provider.received_histories[1] == [
        "My favorite color is blue.",
        "reply-1",
        "What is my favorite color?",
    ]
    runtime.close()

    restarted = SQLiteStorage(database_path)
    loaded = restarted.get_conversation(conversation_id)
    assert [message.content for message in loaded.messages] == [
        "My favorite color is blue.",
        "reply-1",
        "What is my favorite color?",
        "reply-3",
    ]
    restarted.close()


def test_runtime_supports_explicit_conversation_lifecycle(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.db"
    storage = SQLiteStorage(database_path)
    runtime = Runtime(
        Settings(database_path=database_path),
        provider=HistoryProvider(),
        storage=storage,
    )

    conversation = runtime.create_conversation({"name": "explicit"})
    runtime.run("first", conversation_id=conversation.id)
    assert runtime.get_conversation(conversation.id).messages[0].content == "first"
    assert len(runtime.list_conversations()) == 1
    runtime.delete_conversation(conversation.id)
    assert runtime.list_conversations() == []
    runtime.close()


def test_runtime_persists_user_message_when_inference_fails(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.db"
    storage = SQLiteStorage(database_path)
    runtime = Runtime(
        Settings(database_path=database_path),
        provider=FailingProvider(),
        storage=storage,
    )
    conversation = runtime.create_conversation()

    with pytest.raises(ProviderConnectionError):
        runtime.run("keep this user message", conversation_id=conversation.id)

    loaded = storage.get_conversation(conversation.id)
    assert [message.content for message in loaded.messages] == [
        "keep this user message"
    ]
    runtime.close()


def test_runtime_rejects_conversation_and_id_together(tmp_path: Path) -> None:
    runtime = Runtime(
        Settings(database_path=tmp_path / "runtime.db"),
        provider=HistoryProvider(),
        storage=SQLiteStorage(tmp_path / "runtime.db"),
    )

    with pytest.raises(ValueError, match="not both"):
        runtime.run(
            "hello", conversation=runtime.create_conversation(), conversation_id="id"
        )
    runtime.close()
