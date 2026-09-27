from __future__ import annotations

import logging

from core.models import AgentRequest, AgentResponse
from inference.base import InferenceProvider
from tools.base import ToolCall, ToolResult
from tools.permissions import PermissionPolicy
from tools.registry import ToolRegistry


class Agent:
    """Minimal request-to-provider agent for the foundation runtime."""

    def __init__(
        self,
        provider: InferenceProvider,
        name: str = "aegisai-agent",
        *,
        tool_registry: ToolRegistry | None = None,
        permission_policy: PermissionPolicy | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("agent name must be a non-empty string")
        self.provider = provider
        self.name = name.strip()
        self.tool_registry = tool_registry or ToolRegistry(permission_policy)
        self.permission_policy = (
            permission_policy or self.tool_registry.permission_policy
        )
        self._logger = logging.getLogger(__name__)

    def run(self, request: AgentRequest) -> AgentResponse:
        if not isinstance(request, AgentRequest):
            raise TypeError("request must be an AgentRequest")
        self._logger.info(
            "agent request received",
            extra={
                "event": "agent_request",
                "agent": self.name,
                "request_id": request.request_id,
            },
        )
        try:
            response = self.provider.generate(request)
        except Exception:
            self._logger.exception(
                "agent request failed",
                extra={
                    "event": "agent_error",
                    "agent": self.name,
                    "request_id": request.request_id,
                },
            )
            raise
        if response.conversation is None:
            response.conversation = request.conversation
        return response

    def execute_tool(self, call: ToolCall) -> ToolResult:
        if not isinstance(call, ToolCall):
            raise TypeError("call must be a ToolCall")
        return self.tool_registry.execute(
            call.name, call.arguments, self.permission_policy
        )
