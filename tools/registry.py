from collections.abc import Iterable, Mapping
from typing import Any

from core.errors import (
    DuplicateToolError,
    InvalidToolInputError,
    InvalidToolOutputError,
    UnknownToolError,
)
from tools.base import Tool, ToolResult, validate_tool_input, validate_tool_output
from tools.permissions import PermissionPolicy


class ToolRegistry:
    def __init__(self, permission_policy: PermissionPolicy | None = None) -> None:
        self.permission_policy = permission_policy or PermissionPolicy.default()
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if not isinstance(tool, Tool):
            raise TypeError("tool must implement the Tool interface")
        if tool.name in self._tools:
            raise DuplicateToolError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def find(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(f"unknown tool: {name}") from exc

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def validate_requested_tools(self, requested: Iterable[str] | None) -> list[Tool]:
        if requested is None:
            return self.list_tools()
        names = list(requested)
        if len(names) != len(set(names)):
            raise UnknownToolError("requested tool names must be unique")
        return [self.find(name) for name in names]

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        permission_policy: PermissionPolicy | None = None,
    ) -> ToolResult:
        tool = self.find(name)
        policy = permission_policy or self.permission_policy
        policy.require(tool.required_capability)
        validated_input = validate_tool_input(arguments, tool.input_schema)
        result = tool.execute(validated_input)
        if not isinstance(result, ToolResult):
            raise InvalidToolOutputError(
                f"tool {tool.name} did not return a ToolResult"
            )
        try:
            validate_tool_output(result.output, tool.output_schema)
        except InvalidToolInputError as exc:
            raise InvalidToolOutputError(
                f"tool {tool.name} returned invalid output"
            ) from exc
        return result
