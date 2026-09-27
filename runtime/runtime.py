from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Self

from agents.agent import Agent
from core.config import Settings
from core.errors import ConfigurationError
from core.models import AgentRequest, AgentResponse, Conversation, Message, MessageRole
from inference.base import InferenceProvider, ProviderHealth
from inference.ollama import OllamaProvider
from storage.base import ConversationRepository
from storage.sqlite import SQLiteStorage
from tools.permissions import PermissionPolicy
from tools.registry import ToolRegistry


class Runtime:
    """Coordinates persistent synchronous agent requests."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provider: InferenceProvider | None = None,
        agent: Agent | None = None,
        storage: ConversationRepository | None = None,
        tool_registry: ToolRegistry | None = None,
        permission_policy: PermissionPolicy | None = None,
    ) -> None:
        self.settings = settings or Settings.from_environment()
        if provider is None:
            if agent is not None:
                provider = agent.provider
            else:
                if self.settings.inference_provider != "ollama":
                    raise ConfigurationError(
                        f"unsupported inference provider: {self.settings.inference_provider}"
                    )
                provider = OllamaProvider(self.settings)
        self.provider = provider
        self.storage = (
            storage
            if storage is not None
            else SQLiteStorage(self.settings.database_path)
        )
        if agent is None:
            agent = Agent(
                provider,
                name=f"{self.settings.application_name}-agent",
                tool_registry=tool_registry,
                permission_policy=permission_policy,
            )
        self.agent = agent
        self._logger = logging.getLogger(__name__)

    def create_conversation(
        self, metadata: Mapping[str, Any] | None = None
    ) -> Conversation:
        return self.storage.create_conversation(metadata)

    def get_conversation(self, conversation_id: str) -> Conversation:
        return self.storage.get_conversation(conversation_id)

    def list_conversations(self) -> list[Conversation]:
        return self.storage.list_conversations()

    def delete_conversation(self, conversation_id: str) -> None:
        self.storage.delete_conversation(conversation_id)

    def run(
        self,
        user_input: str,
        conversation: Conversation | None = None,
        conversation_id: str | None = None,
    ) -> AgentResponse:
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("user_input must be a non-empty string")
        if conversation is not None and conversation_id is not None:
            raise ValueError("provide conversation or conversation_id, not both")
        if conversation_id is not None:
            current_conversation = self.storage.get_conversation(conversation_id)
        elif conversation is not None:
            current_conversation = conversation
            self.storage.save_conversation(current_conversation)
        else:
            current_conversation = self.storage.create_conversation()
        persisted_message_ids = {
            message.message_id for message in current_conversation.messages
        }
        user_message = current_conversation.add_message(
            Message(role=MessageRole.USER, content=user_input)
        )
        self.storage.append_message(current_conversation.id, user_message)
        persisted_message_ids.add(user_message.message_id)
        request = AgentRequest(
            prompt=user_input,
            conversation=current_conversation,
            metadata={"runtime": self.settings.application_name},
        )
        response = self.agent.run(request)
        response.conversation = current_conversation
        last_message = (
            current_conversation.messages[-1] if current_conversation.messages else None
        )
        if (
            last_message is None
            or last_message.role != MessageRole.ASSISTANT
            or last_message.content != response.content
        ):
            current_conversation.add_message(
                Message(
                    role=MessageRole.ASSISTANT,
                    content=response.content,
                    metadata={
                        "provider": response.provider,
                        "model": response.model,
                    },
                )
            )
        for message in current_conversation.messages:
            if message.message_id not in persisted_message_ids:
                self.storage.append_message(current_conversation.id, message)
                persisted_message_ids.add(message.message_id)
        self._logger.info(
            "runtime request completed",
            extra={
                "event": "runtime_request_completed",
                "request_id": request.request_id,
                "conversation_id": current_conversation.id,
                "user_message_id": user_message.message_id,
                "provider": response.provider,
                "model": response.model,
                "response_length": len(response.content),
            },
        )
        return response

    def health_check(self) -> ProviderHealth:
        return self.provider.health_check()

    def close(self) -> None:
        self.storage.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()
