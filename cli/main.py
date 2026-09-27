from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.config import Settings, load_settings
from core.errors import AegisError, InvalidToolInputError
from observability.logging import configure_logging
from runtime.runtime import Runtime
from tools.calculator import CalculatorTool
from tools.registry import ToolRegistry

COMMAND_NAMES = {"chat", "conversations", "conversation", "health", "tool"}


def _add_runtime_options(
    parser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    default: Any = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--config",
        type=Path,
        default=default,
        help="Optional TOML configuration file",
    )
    parser.add_argument(
        "--environment",
        default=default,
        help="Override the runtime environment",
    )
    parser.add_argument(
        "--log-level",
        default=default,
        help="Override the log level",
    )
    parser.add_argument(
        "--provider",
        default=default,
        help="Override the inference provider",
    )
    parser.add_argument(
        "--model",
        default=default,
        help="Override the model name",
    )
    parser.add_argument(
        "--ollama-base-url",
        default=default,
        help="Override the Ollama base URL",
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        default=default,
        help="Override the SQLite database path",
    )


def _build_legacy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aegisai",
        description="Run one request through the configured local AEGISAI provider.",
    )
    _add_runtime_options(parser)
    parser.add_argument(
        "prompt", nargs="?", help="Prompt to send to the configured model"
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Check whether the configured Ollama server is reachable",
    )
    parser.add_argument("--version", action="version", version="AEGISAI 0.2.0")
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_runtime_options(common, suppress_defaults=True)
    parser = argparse.ArgumentParser(
        prog="aegisai",
        description="Manage AEGISAI conversations and run local requests.",
    )
    _add_runtime_options(parser)
    parser.add_argument(
        "--health",
        action="store_true",
        help="Check whether the configured Ollama server is reachable",
    )
    parser.add_argument("--version", action="version", version="AEGISAI 0.2.0")
    subparsers = parser.add_subparsers(dest="command")

    chat = subparsers.add_parser(
        "chat", parents=[common], help="Create or continue a conversation"
    )
    selection = chat.add_mutually_exclusive_group(required=True)
    selection.add_argument("--new", action="store_true", help="Create a conversation")
    selection.add_argument(
        "--id", dest="conversation_id", help="Continue an existing conversation"
    )
    chat.add_argument("message", nargs="?", help="Message to send")

    conversations = subparsers.add_parser(
        "conversations", parents=[common], help="List stored conversations"
    )
    conversations.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON"
    )

    conversation = subparsers.add_parser(
        "conversation", parents=[common], help="Show one stored conversation"
    )
    conversation.add_argument("conversation_id", help="Conversation ID")

    subparsers.add_parser(
        "health", parents=[common], help="Check the configured Ollama server"
    )

    tool = subparsers.add_parser(
        "tool", parents=[common], help="Execute a registered safe tool"
    )
    tool.add_argument("name", help="Registered tool name")
    tool.add_argument(
        "arguments",
        nargs="?",
        default="{}",
        help="JSON object containing tool arguments",
    )
    tool.add_argument(
        "--expression",
        help="Convenience expression input for the calculator tool",
    )
    return parser


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    overrides = {
        "environment": getattr(args, "environment", None),
        "log_level": getattr(args, "log_level", None),
        "inference_provider": getattr(args, "provider", None),
        "model_name": getattr(args, "model", None),
        "ollama_base_url": getattr(args, "ollama_base_url", None),
        "database_path": getattr(args, "database_path", None),
    }
    values = {key: value for key, value in overrides.items() if value is not None}
    return replace(settings, **values) if values else settings


def _load_runtime_settings(args: argparse.Namespace) -> Settings:
    settings = load_settings(getattr(args, "config", None), environ=os.environ)
    settings = _apply_overrides(settings, args)
    configure_logging(settings.log_level)
    return settings


def _close_runtime(runtime: Runtime) -> None:
    close = getattr(runtime, "close", None)
    if callable(close):
        close()


def _run_health(settings: Settings) -> int:
    runtime = Runtime(settings)
    try:
        health = runtime.health_check()
        print(json.dumps(health.to_dict(), sort_keys=True))
        return 0 if health.reachable else 1
    finally:
        _close_runtime(runtime)


def _run_chat(settings: Settings, args: argparse.Namespace) -> int:
    runtime = Runtime(settings)
    try:
        if args.new:
            conversation = runtime.create_conversation()
            print(f"Conversation ID: {conversation.id}")
            if args.message is None:
                return 0
            response = runtime.run(args.message, conversation_id=conversation.id)
        else:
            if args.message is None:
                print(
                    json.dumps(
                        runtime.get_conversation(args.conversation_id).to_dict(),
                        indent=2,
                    )
                )
                return 0
            response = runtime.run(args.message, conversation_id=args.conversation_id)
        print(response.content)
        return 0
    finally:
        _close_runtime(runtime)


def _run_conversations(settings: Settings, args: argparse.Namespace) -> int:
    runtime = Runtime(settings)
    try:
        conversations = runtime.list_conversations()
        if args.json:
            print(
                json.dumps(
                    [conversation.to_dict() for conversation in conversations],
                    indent=2,
                )
            )
            return 0
        if not conversations:
            print("No conversations.")
            return 0
        print("ID\tUPDATED_AT\tMESSAGES")
        for conversation in conversations:
            print(
                f"{conversation.id}\t{conversation.updated_at.isoformat()}\t"
                f"{len(conversation.messages)}"
            )
        return 0
    finally:
        _close_runtime(runtime)


def _run_conversation(settings: Settings, conversation_id: str) -> int:
    runtime = Runtime(settings)
    try:
        print(json.dumps(runtime.get_conversation(conversation_id).to_dict(), indent=2))
        return 0
    finally:
        _close_runtime(runtime)


def _run_tool(
    settings: Settings,
    name: str,
    arguments_text: str,
    expression: str | None = None,
) -> int:
    if expression is not None:
        if arguments_text != "{}":
            raise InvalidToolInputError(
                "use either JSON arguments or --expression, not both"
            )
        arguments = {"expression": expression}
    else:
        try:
            arguments = json.loads(arguments_text)
        except json.JSONDecodeError as exc:
            raise InvalidToolInputError("tool arguments must be valid JSON") from exc
    if not isinstance(arguments, dict):
        raise InvalidToolInputError("tool arguments must be a JSON object")
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    result = registry.execute(name, arguments)
    print(json.dumps(result.to_dict(), sort_keys=True))
    return 0


def _run_legacy(settings: Settings, args: argparse.Namespace) -> int:
    if args.health:
        return _run_health(settings)
    if args.prompt is None:
        raise ValueError("a prompt is required unless --health is used")
    runtime = Runtime(settings)
    try:
        response = runtime.run(args.prompt)
        print(response.content)
        return 0
    finally:
        _close_runtime(runtime)


def _has_command(argv: Sequence[str]) -> bool:
    options_with_values = {
        "--config",
        "--environment",
        "--log-level",
        "--provider",
        "--model",
        "--ollama-base-url",
        "--database-path",
    }
    expect_value = False
    for token in argv:
        if expect_value:
            expect_value = False
            continue
        if token in COMMAND_NAMES:
            return True
        if token in options_with_values:
            expect_value = True
    return False


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if _has_command(raw_argv):
        parser = build_parser()
        args = parser.parse_args(raw_argv)
        legacy = False
    else:
        parser = _build_legacy_parser()
        args = parser.parse_args(raw_argv)
        legacy = True
    if legacy and not args.health and args.prompt is None:
        parser.print_usage(sys.stderr)
        print(
            "AEGISAI error: a prompt is required unless --health is used",
            file=sys.stderr,
        )
        return 2
    try:
        settings = _load_runtime_settings(args)
        if legacy:
            return _run_legacy(settings, args)
        if args.command == "health" or args.health:
            return _run_health(settings)
        if args.command == "chat":
            return _run_chat(settings, args)
        if args.command == "conversations":
            return _run_conversations(settings, args)
        if args.command == "conversation":
            return _run_conversation(settings, args.conversation_id)
        if args.command == "tool":
            return _run_tool(settings, args.name, args.arguments, args.expression)
        if args.prompt is None:
            parser.print_usage(sys.stderr)
            print(
                "AEGISAI error: a prompt is required unless --health is used or a command is selected",
                file=sys.stderr,
            )
            return 2
        return _run_legacy(settings, args)
    except (AegisError, OSError, TypeError, ValueError) as exc:
        logging.getLogger(__name__).error(
            "CLI request failed",
            extra={"event": "cli_error", "error": str(exc)},
        )
        print(f"AEGISAI error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
