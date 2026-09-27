from __future__ import annotations

import ast
import math
import operator
from collections.abc import Mapping
from typing import Any, cast

from core.errors import InvalidToolInputError
from tools.base import Tool, ToolResult, validate_tool_input
from tools.permissions import Capability


class CalculatorTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="calculator",
            description="Evaluate a small arithmetic expression without using eval.",
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                    }
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"result": {"type": "number"}},
                "required": ["result"],
                "additionalProperties": False,
            },
            required_capability=Capability.READ_ONLY,
        )

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        validated = validate_tool_input(arguments, self.input_schema)
        expression = validated["expression"]
        try:
            tree = ast.parse(expression, mode="eval")
            result = _evaluate(tree)
        except InvalidToolInputError:
            raise
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise InvalidToolInputError("invalid arithmetic expression") from exc
        except (ZeroDivisionError, OverflowError) as exc:
            raise InvalidToolInputError(
                "arithmetic expression cannot be evaluated"
            ) from exc
        return ToolResult.successful(self.name, {"result": result})


def _evaluate(node: ast.AST, depth: int = 0) -> int | float:
    if depth > 32:
        raise InvalidToolInputError("arithmetic expression is too deeply nested")
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, depth + 1)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        value = cast(int | float, node.value)
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidToolInputError("arithmetic result must be finite")
        if isinstance(value, int) and abs(value) > 10**100:
            raise InvalidToolInputError("arithmetic result is too large")
        return value
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, depth + 1)
        if isinstance(node.op, ast.UAdd):
            return value
        if isinstance(node.op, ast.USub):
            return -value
        raise InvalidToolInputError("unsupported unary operator")
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, depth + 1)
        right = _evaluate(node.right, depth + 1)
        if isinstance(node.op, ast.Add):
            result = operator.add(left, right)
        elif isinstance(node.op, ast.Sub):
            result = operator.sub(left, right)
        elif isinstance(node.op, ast.Mult):
            result = operator.mul(left, right)
        elif isinstance(node.op, ast.Div):
            result = operator.truediv(left, right)
        elif isinstance(node.op, ast.Mod):
            result = operator.mod(left, right)
        elif isinstance(node.op, ast.Pow):
            if abs(right) > 12:
                raise InvalidToolInputError("exponent is too large")
            result = operator.pow(left, right)
        else:
            raise InvalidToolInputError("unsupported arithmetic operator")
        if isinstance(result, float) and not math.isfinite(result):
            raise InvalidToolInputError("arithmetic result must be finite")
        if isinstance(result, int) and abs(result) > 10**100:
            raise InvalidToolInputError("arithmetic result is too large")
        return result
    raise InvalidToolInputError("expression contains an unsupported node")
