import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from core.errors import InvalidMessageRoleError


def utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid.uuid4())


def _utc_datetime(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class Message:
    content: str
    role: MessageRole | str = MessageRole.USER
    message_id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("message content must be a string")
        if not isinstance(self.role, MessageRole):
            try:
                role = MessageRole(self.role)
            except (TypeError, ValueError) as exc:
                allowed = ", ".join(item.value for item in MessageRole)
                raise InvalidMessageRoleError(
                    f"message role must be one of: {allowed}"
                ) from exc
            object.__setattr__(self, "role", role)
        if not isinstance(self.message_id, str) or not self.message_id.strip():
            raise ValueError("message_id must be a non-empty string")
        object.__setattr__(
            self, "created_at", _utc_datetime(self.created_at, "created_at")
        )
        if not isinstance(self.metadata, Mapping):
            raise TypeError("message metadata must be a mapping")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": MessageRole(self.role).value,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
            "metadata": dict(self.metadata),
        }

    @property
    def id(self) -> str:
        return self.message_id


@dataclass(slots=True)
class Conversation:
    conversation_id: str = field(default_factory=_new_id)
    messages: list[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.conversation_id, str)
            or not self.conversation_id.strip()
        ):
            raise ValueError("conversation_id must be a non-empty string")
        if not isinstance(self.messages, list) or not all(
            isinstance(message, Message) for message in self.messages
        ):
            raise TypeError("conversation messages must be a list of Message objects")
        object.__setattr__(
            self, "created_at", _utc_datetime(self.created_at, "created_at")
        )
        object.__setattr__(
            self, "updated_at", _utc_datetime(self.updated_at, "updated_at")
        )
        if not isinstance(self.metadata, Mapping):
            raise TypeError("conversation metadata must be a mapping")
        self.metadata = dict(self.metadata)

    def add_message(self, message: Message) -> Message:
        if not isinstance(message, Message):
            raise TypeError("conversation messages must be Message objects")
        self.messages.append(message)
        self.updated_at = message.created_at
        return message

    def append_message(self, message: Message) -> Message:
        return self.add_message(message)

    def get_messages(self) -> list[Message]:
        return list(self.messages)

    def touch(self, timestamp: datetime | None = None) -> None:
        self.updated_at = _utc_datetime(timestamp or utc_now(), "updated_at")

    @property
    def id(self) -> str:
        return self.conversation_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": dict(self.metadata),
            "messages": [message.to_dict() for message in self.messages],
        }


@dataclass(slots=True)
class AgentRequest:
    prompt: str
    request_id: str = field(default_factory=_new_id)
    conversation: Conversation | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("agent request prompt must be a non-empty string")
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        if self.conversation is not None and not isinstance(
            self.conversation, Conversation
        ):
            raise TypeError("conversation must be a Conversation or None")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("agent request metadata must be a mapping")
        self.metadata = dict(self.metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "prompt": self.prompt,
            "conversation": self.conversation.to_dict() if self.conversation else None,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class AgentResponse:
    content: str
    request_id: str
    provider: str
    model: str
    response_id: str = field(default_factory=_new_id)
    conversation: Conversation | None = None
    created_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("agent response content must be a string")
        for name, value in (
            ("request_id", self.request_id),
            ("provider", self.provider),
            ("model", self.model),
            ("response_id", self.response_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.conversation is not None and not isinstance(
            self.conversation, Conversation
        ):
            raise TypeError("conversation must be a Conversation or None")
        object.__setattr__(
            self, "created_at", _utc_datetime(self.created_at, "created_at")
        )
        if not isinstance(self.metadata, Mapping):
            raise TypeError("agent response metadata must be a mapping")
        self.metadata = dict(self.metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "response_id": self.response_id,
            "request_id": self.request_id,
            "content": self.content,
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at.isoformat(),
            "conversation": self.conversation.to_dict() if self.conversation else None,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class Event:
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=_new_id)
    timestamp: datetime = field(default_factory=utc_now)
    source: str = "aegisai"

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise ValueError("event_type must be a non-empty string")
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("event_id must be a non-empty string")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty string")
        if not isinstance(self.payload, Mapping):
            raise TypeError("event payload must be a mapping")
        self.payload = dict(self.payload)
        object.__setattr__(
            self, "timestamp", _utc_datetime(self.timestamp, "timestamp")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "payload": dict(self.payload),
        }
