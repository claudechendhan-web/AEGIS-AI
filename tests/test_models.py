from datetime import UTC, datetime

import pytest

from core.models import (
    AgentRequest,
    AgentResponse,
    Conversation,
    Event,
    Message,
    MessageRole,
)


def test_message_conversation_and_serialization() -> None:
    message = Message(content="Hello", role="user")
    conversation = Conversation()
    conversation.add_message(message)

    assert message.role is MessageRole.USER
    assert conversation.messages == [message]
    assert message.to_dict()["role"] == "user"
    assert conversation.to_dict()["messages"][0]["content"] == "Hello"


def test_agent_request_and_response_serialization() -> None:
    conversation = Conversation(
        messages=[Message(content="Hello", role=MessageRole.USER)]
    )
    request = AgentRequest(prompt="Hello", conversation=conversation)
    response = AgentResponse(
        content="Hi",
        request_id=request.request_id,
        provider="ollama",
        model="qwen2.5-coder:7b",
        conversation=conversation,
    )

    assert (
        request.to_dict()["conversation"]["conversation_id"]
        == conversation.conversation_id
    )
    assert response.to_dict()["request_id"] == request.request_id
    assert response.to_dict()["conversation"]["messages"][0]["content"] == "Hello"


def test_event_uses_utc_timestamp() -> None:
    timestamp = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    event = Event(event_type="test.event", payload={"value": 1}, timestamp=timestamp)

    assert event.timestamp.tzinfo == UTC
    assert event.to_dict()["event_type"] == "test.event"
    assert event.to_dict()["payload"] == {"value": 1}


def test_request_rejects_empty_prompt() -> None:
    with pytest.raises(ValueError, match="prompt"):
        AgentRequest(prompt="  ")
