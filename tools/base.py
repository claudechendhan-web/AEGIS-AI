import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.errors import InvalidToolInputError
from tools.permissions import Capability


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_name: str
    output: Mapping[str, Any] = field(default_factory=dict)
    success: bool = True
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")
        if not isinstance(self.output, Mapping):
            raise TypeError("tool output must be a mapping")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("tool metadata must be a mapping")
        object.__setattr__(self, "output", dict(self.output))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @classmethod
    def successful(
        cls,
        tool_name: str,
        output: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(tool_name, output, True, None, metadata or {})

    @classmethod
    def failed(
        cls,
        tool_name: str,
        error: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(tool_name, {}, False, error, metadata or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "output": dict(self.output),
            "success": self.success,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool name must be a non-empty string")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("tool arguments must be a mapping")
        object.__setattr__(self, "arguments", dict(self.arguments))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": dict(self.arguments),
            "call_id": self.call_id,
        }


class Tool(ABC):
    def __init__(
        self,
        name: str,
        description: str,
        input_schema: Mapping[str, Any],
        output_schema: Mapping[str, Any],
        required_capability: Capability | str,
    ) -> None:
        for field_name, value in (("name", name), ("description", description)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"tool {field_name} must be a non-empty string")
        if not isinstance(input_schema, Mapping) or not isinstance(
            output_schema, Mapping
        ):
            raise TypeError("tool schemas must be mappings")
        self.name = name.strip()
        self.description = description.strip()
        self.input_schema = dict(input_schema)
        self.output_schema = dict(output_schema)
        self.required_capability = Capability(required_capability)

    @abstractmethod
    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        raise NotImplementedError


def validate_tool_input(
    arguments: Mapping[str, Any], schema: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise InvalidToolInputError("tool input must be an object")
    validated = _validate_value(arguments, schema, "input")
    if not isinstance(validated, dict):
        raise InvalidToolInputError("tool input must be an object")
    return validated


def validate_tool_output(
    output: Mapping[str, Any], schema: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(output, Mapping):
        raise InvalidToolInputError("tool output must be an object")
    validated = _validate_value(output, schema, "output")
    if not isinstance(validated, dict):
        raise InvalidToolInputError("tool output must be an object")
    return validated


def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> Any:
    expected_type = schema.get("type")
    if expected_type is not None:
        expected_types = (
            set(expected_type) if isinstance(expected_type, list) else {expected_type}
        )
        if not any(_matches_type(value, item) for item in expected_types):
            expected = ", ".join(sorted(str(item) for item in expected_types))
            raise InvalidToolInputError(f"{path} must have type {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidToolInputError(f"{path} is not an allowed value")
    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        if minimum_length is not None and len(value) < minimum_length:
            raise InvalidToolInputError(f"{path} is too short")
        if maximum_length is not None and len(value) > maximum_length:
            raise InvalidToolInputError(f"{path} is too long")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and not math.isfinite(float(value))
    ):
        raise InvalidToolInputError(f"{path} must be finite")
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise InvalidToolInputError("tool schema properties are invalid")
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise InvalidToolInputError("tool schema required fields are invalid")
        for name in required:
            if not isinstance(name, str) or name not in value:
                raise InvalidToolInputError(f"{path}.{name} is required")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                names = ", ".join(unknown)
                raise InvalidToolInputError(f"{path} contains unknown fields: {names}")
        validated_mapping: dict[str, Any] = {}
        for name, item in value.items():
            property_schema = properties.get(name)
            if isinstance(property_schema, Mapping):
                validated_mapping[name] = _validate_value(
                    item, property_schema, f"{path}.{name}"
                )
            else:
                validated_mapping[name] = item
        return validated_mapping
    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            return [
                _validate_value(item, item_schema, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
    return value


def _matches_type(value: Any, expected: Any) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False
