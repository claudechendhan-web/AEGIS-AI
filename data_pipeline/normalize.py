from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from data_pipeline.common import (
    Checkpoint,
    PipelineReport,
    SourceContext,
    canonical_json,
    iter_input_records,
    remaining_record_limit,
    stable_hash,
    write_json,
)
from data_pipeline.config import PipelineConfig, PipelineError


def normalize_record(
    record: Any, context: SourceContext, row_number: int
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(record, Mapping):
        return None, "record_not_object"
    if "messages" in record:
        messages = _normalize_messages(record.get("messages"))
        if messages is None:
            return None, "invalid_messages"
        source_metadata = record.get("metadata")
        metadata = _metadata(context, row_number, record, source_metadata)
    elif "content" in record and isinstance(record.get("content"), list):
        messages = _normalize_action_content(record.get("content"))
        if not messages:
            return None, "empty_action_content"
        metadata = _metadata(context, row_number, record, record.get("details"))
    elif "conversations" in record:
        messages = _normalize_conversations(record)
        if not messages:
            return None, "empty_conversations"
        metadata = _metadata(context, row_number, record, None)
    elif "instruction" in record and "text" in record:
        instruction = record.get("instruction")
        text = record.get("text")
        identifier = record.get("id")
        if not isinstance(instruction, str) or not instruction.strip():
            return None, "invalid_instruction"
        if not isinstance(text, str) or not text.strip():
            return None, "invalid_text"
        messages = [
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": text},
        ]
        metadata = _metadata(context, row_number, record, record.get("metadata"))
        identifier = identifier if isinstance(identifier, str) else stable_hash(record)
    else:
        return None, "missing_supported_structure"
    if not messages:
        return None, "empty_messages"
    tool_calls = [
        call
        for message in messages
        for call in message.get("tool_calls", [])
        if isinstance(call, Mapping)
    ]
    tool_results = [
        {
            "role": message["role"],
            "content": message.get("content", ""),
        }
        for message in messages
        if message.get("role") == "tool"
    ]
    identifier = _identifier(record, context, row_number)
    metadata["agentic_flow"] = _agentic_flow(messages, tool_calls, tool_results)
    normalized = {
        "id": _qualified_identifier(identifier, context),
        "source": context.source,
        "task_type": None,
        "messages": messages,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "metadata": metadata,
        "license": _record_license(record, context),
        "quality_score": None,
    }
    return normalized, None


def normalize_jsonl(
    input_path: str | Path,
    output_path: str | Path,
    *,
    context: SourceContext,
    report_path: str | Path,
    rejects_path: str | Path,
    max_records: int = 0,
    resume: bool = True,
) -> PipelineReport:
    if max_records < 0:
        raise PipelineError("max_records must be zero or greater")
    input_file = Path(input_path)
    output = Path(output_path)
    rejects = Path(rejects_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    rejects.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = Checkpoint.load(Path(report_path).with_suffix(".checkpoint.json"))
    start_line = checkpoint.get_int("last_line") if resume else 0
    if not resume:
        checkpoint.state = {}
    record_limit = remaining_record_limit(max_records, start_line)
    report = PipelineReport(
        stage="normalize",
        source=context.source,
        input_path=str(input_file),
        output_path=str(output),
    )
    with (
        output.open(
            "a" if resume else "w", encoding="utf-8", newline="\n"
        ) as output_stream,
        rejects.open(
            "a" if resume else "w", encoding="utf-8", newline="\n"
        ) as reject_stream,
    ):
        for line_number, record, parse_error in iter_input_records(
            input_file, context.format, start_line=start_line
        ):
            if record_limit == 0:
                break
            report.increment("records_seen")
            if parse_error is not None:
                report.increment("malformed")
                report.errors.append(f"line {line_number}: {parse_error}")
                _write_reject(reject_stream, line_number, parse_error, record)
            else:
                normalized, reason = normalize_record(record, context, line_number)
                if normalized is None:
                    report.increment("rejected")
                    report.increment(reason or "invalid")
                    _write_reject(
                        reject_stream, line_number, reason or "invalid", record
                    )
                else:
                    output_stream.write(canonical_json(normalized))
                    output_stream.write("\n")
                    report.increment("accepted")
            output_stream.flush()
            reject_stream.flush()
            checkpoint.state = {"last_line": line_number}
            checkpoint.save()
            if (
                record_limit is not None
                and report.counters.get("records_seen", 0) >= record_limit
            ):
                break
    write_json(report_path, report.finish().to_dict())
    return report


def _normalize_messages(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list) or not value:
        return None
    messages: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError:
                return None
        if not isinstance(item, Mapping):
            return None
        role = _role(item.get("role"))
        if role is None:
            return None
        content = _content(item.get("content"))
        if content is None:
            return None
        message: dict[str, Any] = {"role": role, "content": content}
        calls = item.get("tool_calls")
        if isinstance(calls, list):
            normalized_calls = [_normalize_tool_call(call) for call in calls]
            if any(call is None for call in normalized_calls):
                return None
            message["tool_calls"] = [call for call in normalized_calls if call]
        messages.append(message)
    return messages


def _normalize_action_content(value: Any) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        class_name = item.get("class_")
        content = _content(item.get("content") or item.get("html"))
        if class_name == "text_observation":
            role = "user" if item.get("source") == "user" else "tool"
            if content is not None:
                messages.append({"role": role, "content": content})
        elif class_name == "message_action":
            if content is not None:
                messages.append({"role": "assistant", "content": content})
        elif class_name in {"api_action", "code_action"}:
            function = item.get("function")
            arguments = item.get("kwargs")
            if class_name == "code_action":
                function = "code_action"
                arguments = {"code": content}
            if isinstance(function, str) and function:
                messages.append(
                    {
                        "role": "assistant",
                        "content": _content(item.get("description")) or "",
                        "tool_calls": [
                            {
                                "name": function,
                                "arguments": arguments
                                if isinstance(arguments, Mapping)
                                else {},
                            }
                        ],
                    }
                )
        elif class_name in {"web_observation", "observation"}:
            if content is not None:
                messages.append({"role": "tool", "content": content})
    return messages


def _normalize_conversations(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = record.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    conversations = record.get("conversations")
    if not isinstance(conversations, list):
        return []
    for turn in conversations:
        if not isinstance(turn, Mapping):
            continue
        role = _role(turn.get("from"))
        content = _content(turn.get("value"))
        if role is None or content is None:
            continue
        message: dict[str, Any] = {"role": role, "content": content}
        function_match = re.search(
            r"<function=([^>]+)>\s*<parameter=([^>]+)>\s*(.*?)\s*</parameter>",
            content,
            flags=re.DOTALL,
        )
        if function_match:
            message["tool_calls"] = [
                {
                    "name": function_match.group(1).strip(),
                    "arguments": {
                        function_match.group(2).strip(): function_match.group(3).strip()
                    },
                }
            ]
        messages.append(message)
    return messages


def _normalize_tool_call(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    function = value.get("function")
    if isinstance(function, Mapping):
        name = function.get("name")
        arguments = function.get("arguments")
    else:
        name = value.get("name")
        arguments = value.get("arguments", value.get("input", {}))
    if not isinstance(name, str) or not name.strip():
        return None
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {"raw": arguments}
    if not isinstance(arguments, Mapping):
        arguments = {"raw": arguments}
    call: dict[str, Any] = {"name": name.strip(), "arguments": dict(arguments)}
    call_id = value.get("id") or value.get("call_id")
    if isinstance(call_id, str) and call_id:
        call["id"] = call_id
    return call


def _qualified_identifier(identifier: str, context: SourceContext) -> str:
    alias = context.extra.get("source_alias")
    file_hash = context.extra.get("raw_file_sha256")
    file_part = str(file_hash)[:12] if isinstance(file_hash, str) else ""
    qualifier = ":".join(
        part
        for part in (context.source, alias, file_part)
        if isinstance(part, str) and part
    )
    if identifier.startswith(f"{qualifier}:"):
        return identifier
    if identifier.startswith(f"{context.source}:"):
        suffix = ":".join(
            part
            for part in (alias, str(file_hash)[:12])
            if isinstance(part, str) and part and part != "None"
        )
        return f"{identifier}:{suffix}" if suffix else identifier
    return f"{qualifier}:{identifier}"


def _agentic_flow(
    messages: list[dict[str, Any]], tool_calls: list[Any], tool_results: list[Any]
) -> dict[str, Any]:
    text = " ".join(
        str(message.get("content", ""))
        for message in messages
        if isinstance(message, Mapping)
    ).lower()
    roles = [message.get("role") for message in messages]
    last_assistant = next(
        (
            message
            for message in reversed(messages)
            if message.get("role") == "assistant"
        ),
        None,
    )
    return {
        "user_goal_present": "user" in roles,
        "plan_evidence": bool(re.search(r"\b(plan|step|decompose|roadmap)\b", text)),
        "tool_selection_count": len(tool_calls),
        "permission_check": "not_present_in_source",
        "tool_result_count": len(tool_results),
        "observation_count": sum(role == "tool" for role in roles),
        "next_action_evidence": any(
            role == "assistant" and index > 0 and roles[index - 1] == "tool"
            for index, role in enumerate(roles)
        ),
        "completion_evidence": bool(
            isinstance(last_assistant, Mapping)
            and str(last_assistant.get("content", "")).strip()
            and not last_assistant.get("tool_calls")
        ),
    }


def _metadata(
    context: SourceContext,
    row_number: int,
    record: Mapping[str, Any],
    extra: Any,
) -> dict[str, Any]:
    metadata = context.provenance(row_number)
    if isinstance(extra, Mapping):
        metadata["source_metadata"] = dict(extra)
    details = record.get("details")
    if isinstance(details, Mapping):
        metadata["details"] = dict(details)
    for field_name in ("tools", "reasoning", "used_in"):
        if field_name in record:
            metadata[field_name] = record[field_name]
    return metadata


def _identifier(
    record: Mapping[str, Any], context: SourceContext, row_number: int
) -> str:
    for field_name in ("id", "uuid"):
        value = record.get(field_name)
        if isinstance(value, str) and value.strip():
            return value
    return f"row-{row_number}-{stable_hash(record)[:16]}"


def _record_license(record: Mapping[str, Any], context: SourceContext) -> str:
    value = record.get("license")
    return value if isinstance(value, str) and value.strip() else context.license_name


def _role(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return {
        "human": "user",
        "user": "user",
        "gpt": "assistant",
        "assistant": "assistant",
        "system": "system",
        "tool": "tool",
        "observation": "tool",
        "environment": "tool",
    }.get(value.lower())


def _content(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (Mapping, list)):
        return canonical_json(value)
    return str(value)


def _write_reject(stream: Any, line_number: int, reason: str, raw: Any) -> None:
    stream.write(
        canonical_json({"row_number": line_number, "reason": reason, "raw": raw})
    )
    stream.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize raw agent data records")
    parser.add_argument("--config", type=Path, default=Path("configs/dataset.toml"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--format", default="jsonl")
    parser.add_argument("--license", default="unknown")
    parser.add_argument("--revision", default="unknown")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--rejects", type=Path, required=True)
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument("--no-resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        PipelineConfig.load(args.config)
        context = SourceContext(
            source=args.source,
            license_name=args.license,
            revision=args.revision,
            file_name=str(args.input),
            format=args.format,
        )
        report = normalize_jsonl(
            args.input,
            args.output,
            context=context,
            report_path=args.report,
            rejects_path=args.rejects,
            max_records=args.max_records,
            resume=not args.no_resume,
        )
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    except (PipelineError, OSError, ValueError) as exc:
        print(f"Normalization error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
