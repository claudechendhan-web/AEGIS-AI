import pytest

from core.errors import (
    DuplicateToolError,
    InvalidToolInputError,
    InvalidToolOutputError,
    PermissionDeniedError,
    UnknownToolError,
)
from tools.base import Tool, ToolResult
from tools.calculator import CalculatorTool
from tools.permissions import Capability, PermissionPolicy
from tools.registry import ToolRegistry


class WriteTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="write-test",
            description="A test tool requiring write capability.",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            required_capability=Capability.FILESYSTEM_WRITE,
        )

    def execute(self, arguments):
        return ToolResult.successful(self.name, {"written": True})


class InvalidOutputTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="invalid-output",
            description="A test tool with an invalid output.",
            input_schema={"type": "object"},
            output_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
            required_capability=Capability.READ_ONLY,
        )

    def execute(self, arguments):
        return ToolResult.successful(self.name, {"value": "not-an-integer"})


def test_registry_registers_finds_and_lists_tools() -> None:
    registry = ToolRegistry()
    tool = CalculatorTool()

    assert registry.register(tool) is tool
    assert registry.find("calculator") is tool
    assert registry.list_tools() == [tool]
    assert registry.validate_requested_tools(["calculator"]) == [tool]


def test_registry_rejects_duplicate_and_unknown_tools() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())

    with pytest.raises(DuplicateToolError):
        registry.register(CalculatorTool())
    with pytest.raises(UnknownToolError):
        registry.find("missing")
    with pytest.raises(UnknownToolError):
        registry.validate_requested_tools(["missing"])


def test_calculator_evaluates_arithmetic_without_eval() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())

    result = registry.execute("calculator", {"expression": "2 + 3 * 4"})

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.output == {"result": 14}


def test_calculator_rejects_invalid_input_and_expressions() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())

    for arguments in (
        {},
        {"expression": "__import__('os').system('echo unsafe')"},
        {"expression": "1 / 0"},
        {"expression": "2 ^ 3"},
        {"expression": "2 + 3", "extra": True},
    ):
        with pytest.raises(InvalidToolInputError):
            registry.execute("calculator", arguments)


def test_permission_denied_for_unrequested_capability() -> None:
    registry = ToolRegistry()
    registry.register(WriteTool())

    with pytest.raises(PermissionDeniedError, match="FILESYSTEM_WRITE"):
        registry.execute("write-test", {})


def test_process_execution_is_permanently_disabled() -> None:
    with pytest.raises(PermissionDeniedError, match="PROCESS_EXECUTION"):
        PermissionPolicy({Capability.PROCESS_EXECUTION})

    policy = PermissionPolicy()
    assert policy.allows(Capability.READ_ONLY) is True
    assert policy.allows(Capability.PROCESS_EXECUTION) is False
    assert policy.to_dict()["process_execution"] is False


def test_tool_output_schema_is_enforced() -> None:
    registry = ToolRegistry()
    registry.register(InvalidOutputTool())

    with pytest.raises(InvalidToolOutputError):
        registry.execute("invalid-output", {})
