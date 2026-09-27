import json
from email.message import Message as EmailMessage
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from core.config import Settings
from core.errors import ProviderConnectionError, ProviderResponseError
from core.models import AgentRequest, Conversation, Message, MessageRole
from inference.ollama import OllamaProvider


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, result: FakeResponse | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[Request, float]] = []

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def make_provider(
    result: FakeResponse | BaseException,
) -> tuple[OllamaProvider, FakeOpener]:
    opener = FakeOpener(result)
    return OllamaProvider(Settings(), timeout=3.5, opener=opener), opener


def test_generate_posts_prompt_to_ollama() -> None:
    provider, opener = make_provider(
        FakeResponse({"response": "Hello from Ollama", "done": True})
    )
    conversation = Conversation()
    request = AgentRequest(prompt="Hello", conversation=conversation)

    response = provider.generate(request)

    http_request, timeout = opener.calls[0]
    assert isinstance(http_request.data, bytes)
    payload = json.loads(http_request.data.decode("utf-8"))
    assert timeout == 3.5
    assert http_request.full_url == "http://127.0.0.1:11434/api/generate"
    assert http_request.method == "POST"
    assert payload == {
        "model": "qwen2.5-coder:3b",
        "prompt": "Hello",
        "stream": False,
    }
    assert response.content == "Hello from Ollama"
    assert response.provider == "ollama"
    assert response.model == "qwen2.5-coder:3b"
    assert conversation.messages[0].role is MessageRole.ASSISTANT


def test_generate_sends_previous_conversation_history() -> None:
    provider, opener = make_provider(FakeResponse({"response": "follow-up response"}))
    conversation = Conversation(
        messages=[
            Message(content="My favorite color is blue.", role=MessageRole.USER),
            Message(content="Noted.", role=MessageRole.ASSISTANT),
        ]
    )
    request = AgentRequest(
        prompt="What is my favorite color?", conversation=conversation
    )

    provider.generate(request)

    http_request, _ = opener.calls[0]
    assert isinstance(http_request.data, bytes)
    payload = json.loads(http_request.data.decode("utf-8"))
    assert "user: My favorite color is blue." in payload["prompt"]
    assert "assistant: Noted." in payload["prompt"]
    assert "user: What is my favorite color?" in payload["prompt"]


def test_generate_wraps_connection_errors() -> None:
    provider, _ = make_provider(URLError("connection refused"))

    with pytest.raises(ProviderConnectionError, match="unable to connect"):
        provider.generate(AgentRequest(prompt="Hello"))


def test_generate_wraps_http_errors() -> None:
    error = HTTPError(
        "http://127.0.0.1:11434/api/generate",
        503,
        "unavailable",
        EmailMessage(),
        None,
    )
    provider, _ = make_provider(error)

    with pytest.raises(ProviderResponseError, match="HTTP 503"):
        provider.generate(AgentRequest(prompt="Hello"))


def test_generate_rejects_invalid_payloads() -> None:
    provider, _ = make_provider(FakeResponse({"error": "bad request"}))

    with pytest.raises(ProviderResponseError, match="text response"):
        provider.generate(AgentRequest(prompt="Hello"))


def test_health_check_reports_reachable_server() -> None:
    provider, opener = make_provider(
        FakeResponse({"models": [{"name": "qwen2.5-coder:7b"}]})
    )

    health = provider.health_check()

    assert health.reachable is True
    assert health.healthy is True
    assert "reachable" in health.detail
    assert health.latency_ms is not None
    assert opener.calls[0][0].full_url == "http://127.0.0.1:11434/api/tags"
    assert opener.calls[0][0].method == "GET"


def test_health_check_reports_unreachable_server() -> None:
    provider, _ = make_provider(URLError("connection refused"))

    health = provider.health_check()

    assert health.reachable is False
    assert health.healthy is False
    assert "unavailable" in health.detail


def test_custom_base_url_and_model_are_used() -> None:
    provider = OllamaProvider(
        Settings(ollama_base_url="http://localhost:11435", model_name="local-model")
    )

    assert provider.base_url == "http://localhost:11435"
    assert provider.model_name == "local-model"
