import pytest

from core.config import Settings
from core.errors import ProviderConnectionError
from core.models import AgentRequest, AgentResponse, Conversation, MessageRole
from inference.base import InferenceProvider, ProviderHealth
from runtime.runtime import Runtime


class RecordingProvider(InferenceProvider):
    @property
    def name(self) -> str:
        return "recording"

    def generate(self, request):
        self.requests.append(request)
        return AgentResponse(
            content="recorded response",
            request_id=request.request_id,
            provider=self.name,
            model="recording-model",
            conversation=request.conversation,
        )

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(self.name, "recording-model", True, "recording available")

    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []


class FailingProvider(RecordingProvider):
    def generate(self, request):
        raise ProviderConnectionError("recording provider is offline")


def test_runtime_routes_user_input_through_agent_and_provider() -> None:
    provider = RecordingProvider()
    conversation = Conversation()
    runtime = Runtime(Settings(), provider=provider)

    response = runtime.run("Hello", conversation=conversation)

    assert response.content == "recorded response"
    assert provider.requests[0].prompt == "Hello"
    assert provider.requests[0].conversation is conversation
    assert [message.role for message in conversation.messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert conversation.messages[0].content == "Hello"
    assert conversation.messages[1].content == "recorded response"


def test_runtime_creates_conversation_when_none_is_supplied() -> None:
    provider = RecordingProvider()
    runtime = Runtime(Settings(), provider=provider)

    response = runtime.run("Hello")

    assert response.conversation is not None
    assert len(response.conversation.messages) == 2


def test_runtime_propagates_provider_errors() -> None:
    runtime = Runtime(Settings(), provider=FailingProvider())

    with pytest.raises(ProviderConnectionError, match="offline"):
        runtime.run("Hello")


def test_runtime_health_check_delegates_to_provider() -> None:
    provider = RecordingProvider()
    runtime = Runtime(Settings(), provider=provider)

    health = runtime.health_check()

    assert health.provider == "recording"
    assert health.reachable is True
