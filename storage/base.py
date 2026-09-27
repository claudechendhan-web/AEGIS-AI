from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from core.models import Conversation, Message


class ConversationRepository(ABC):
    """Persistence contract for conversations and their ordered messages."""

    @abstractmethod
    def create_conversation(
        self, metadata: Mapping[str, Any] | None = None
    ) -> Conversation:
        raise NotImplementedError

    @abstractmethod
    def get_conversation(self, conversation_id: str) -> Conversation:
        raise NotImplementedError

    @abstractmethod
    def list_conversations(self) -> list[Conversation]:
        raise NotImplementedError

    @abstractmethod
    def delete_conversation(self, conversation_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def append_message(self, conversation_id: str, message: Message) -> Message:
        raise NotImplementedError

    @abstractmethod
    def get_messages(self, conversation_id: str) -> list[Message]:
        raise NotImplementedError

    @abstractmethod
    def save_conversation(self, conversation: Conversation) -> Conversation:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class Storage(ConversationRepository, ABC):
    """Alias-style base for storage implementations."""
