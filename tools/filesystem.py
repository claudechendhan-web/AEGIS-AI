"""Filesystem tools: Read, Write, Edit, Ls, Glob, Grep.

Every path is resolved and confined under a single root before any IO happens,
so a `../../` escape or a symlink out of the tree fails closed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools.base import Tool, ToolResult
from tools.permissions import Capability

MAX_READ_CHARS = 20_000
MAX_WRITE_CHARS = 400_000
MAX_LIST_ENTRIES = 500
MAX_GREP_MATCHES = 200
IGNORED_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    ".mypy_cache",
    ".ruff_cache",
}


class _ConfinedTool(Tool):
    def __init__(
        self,
        name: str,
        description: str,
        input_schema: Mapping[str, Any],
        output_schema: Mapping[str, Any],
        capability: Capability,
        root: Path | None,
    ) -> None:
        super().__init__(name, description, input_schema, output_schema, capability)
        self.root = root.resolve() if root is not None else None

    def resolve(self, candidate: str) -> Path:
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("path must be a non-empty string")
        target = (
            (self.root / candidate).resolve()
            if self.root is not None and not os.path.isabs(candidate)
            else Path(candidate).resolve()
        )
        if self.root is None:
            return target
        try:
            target.relative_to(self.root)
        except ValueError:
            raise PermissionError(
                f"path escapes the workspace root: {candidate}"
            ) from None
        return target

    def _display(self, target: Path) -> str:
        """Render a path relative to the root when confined, else absolute."""
        if self.root is not None:
            try:
                return str(target.relative_to(self.root)).replace("\\", "/")
            except ValueError:
                pass
        return str(target).replace("\\", "/")

    def _base(self, pattern: str) -> Any:
        """Glob from the root when confined, else from the drive/cwd root."""
        if self.root is not None:
            return self.root.glob(pattern)
        head = Path(pattern.split("/", 1)[0].split("*", 1)[0].rstrip("/") or ".")
        if head.is_absolute():
            return Path(head.anchor or ".").glob(pattern)
        return Path.cwd().glob(pattern)


class ReadTool(_ConfinedTool):
    def __init__(self, root: Path | None) -> None:
        super().__init__(
            "read_file",
            "Read a UTF-8 text file from the workspace. Returns numbered lines.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "1-based first line",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Max lines to return",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            {"type": "object", "properties": {"result": {"type": "string"}}},
            Capability.FILESYSTEM_READ,
            root,
        )

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        path = self.resolve(arguments["path"])
        if not path.is_file():
            return ToolResult.failed(self.name, f"not a file: {arguments['path']}")
        text = path.read_text(encoding="utf-8", errors="replace")
        offset = int(arguments.get("offset", 1))
        limit = int(arguments.get("limit", 2000))
        lines = text.splitlines()
        selected = lines[offset - 1 : offset - 1 + limit]
        body = "\n".join(f"{offset + i:>5}\t{line}" for i, line in enumerate(selected))
        truncated = False
        if len(body) > MAX_READ_CHARS:
            body = body[:MAX_READ_CHARS]
            truncated = True
        suffix = "\n... [truncated]" if truncated else ""
        more = ""
        if offset - 1 + limit < len(lines):
            more = f"\n... [more lines: re-read with offset={offset + limit}]"
        return ToolResult.successful(
            self.name,
            {
                "result": f"{body}{suffix}{more}",
                "lines": len(lines),
                "truncated": truncated,
            },
        )


class WriteTool(_ConfinedTool):
    def __init__(self, root: Path | None) -> None:
        super().__init__(
            "write_file",
            "Create or overwrite a UTF-8 text file in the workspace.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "written": {"type": "boolean"},
                    "bytes": {"type": "integer"},
                },
            },
            Capability.FILESYSTEM_WRITE,
            root,
        )

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        path = self.resolve(arguments["path"])
        content = arguments["content"]
        if not isinstance(content, str):
            return ToolResult.failed(self.name, "content must be a string")
        if len(content) > MAX_WRITE_CHARS:
            return ToolResult.failed(
                self.name, f"content exceeds {MAX_WRITE_CHARS} characters"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(content, encoding="utf-8")
        return ToolResult.successful(
            self.name,
            {
                "written": True,
                "created": not existed,
                "bytes": len(content.encode("utf-8")),
            },
        )


class EditTool(_ConfinedTool):
    def __init__(self, root: Path | None) -> None:
        super().__init__(
            "edit_file",
            "Replace an exact string in an existing file. old_string must appear exactly once unless replace_all is true.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
            {"type": "object", "properties": {"edits": {"type": "integer"}}},
            Capability.FILESYSTEM_WRITE,
            root,
        )

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        old = arguments["old_string"]
        new = arguments["new_string"]
        if old == new:
            return ToolResult.failed(
                self.name, "no changes: old_string and new_string are identical"
            )
        path = self.resolve(arguments["path"])
        if not path.is_file():
            return ToolResult.failed(self.name, f"not a file: {arguments['path']}")
        text = path.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        if count == 0:
            return ToolResult.failed(self.name, "old_string not found in file")
        if count > 1 and not arguments.get("replace_all"):
            return ToolResult.failed(
                self.name, f"old_string appears {count} times; pass replace_all"
            )
        updated = (
            text.replace(old, new)
            if arguments.get("replace_all")
            else text.replace(old, new, 1)
        )
        path.write_text(updated, encoding="utf-8")
        return ToolResult.successful(
            self.name, {"edits": count if arguments.get("replace_all") else 1}
        )


class LsTool(_ConfinedTool):
    def __init__(self, root: Path | None) -> None:
        super().__init__(
            "list_dir",
            "List files and directories under a workspace path.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "recursive": {"type": "boolean"},
                },
                "required": [],
                "additionalProperties": False,
            },
            {"type": "object", "properties": {"entries": {"type": "array"}}},
            Capability.FILESYSTEM_READ,
            root,
        )

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        target = self.resolve(arguments.get("path", "."))
        if not target.is_dir():
            return ToolResult.failed(
                self.name, f"not a directory: {arguments.get('path', '.')}"
            )
        pattern = "**/*" if arguments.get("recursive") else "*"
        entries: list[str] = []
        for item in sorted(target.glob(pattern)):
            if any(part in IGNORED_DIRS for part in item.parts):
                continue
            entries.append(self._display(item))
            if len(entries) >= MAX_LIST_ENTRIES:
                break
        return ToolResult.successful(
            self.name, {"entries": entries, "count": len(entries)}
        )


class GlobTool(_ConfinedTool):
    def __init__(self, root: Path | None) -> None:
        super().__init__(
            "glob",
            "Find workspace files matching a glob pattern such as **/*.py.",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            {"type": "object", "properties": {"matches": {"type": "array"}}},
            Capability.FILESYSTEM_READ,
            root,
        )

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        pattern = arguments["pattern"]
        limit = int(arguments.get("limit", 200))
        matches: list[str] = []
        for item in sorted(self._base(pattern)):
            if not item.is_file() or any(p in IGNORED_DIRS for p in item.parts):
                continue
            matches.append(self._display(item))
            if len(matches) >= limit:
                break
        return ToolResult.successful(
            self.name, {"matches": matches, "count": len(matches)}
        )


class GrepTool(_ConfinedTool):
    def __init__(self, root: Path | None) -> None:
        super().__init__(
            "grep",
            "Search workspace file contents with a regular expression.",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            {"type": "object", "properties": {"matches": {"type": "array"}}},
            Capability.FILESYSTEM_READ,
            root,
        )

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        try:
            regex = re.compile(arguments["pattern"])
        except re.error as exc:
            return ToolResult.failed(self.name, f"invalid regular expression: {exc}")
        glob = arguments.get("glob", "**/*")
        limit = int(arguments.get("limit", MAX_GREP_MATCHES))
        matches: list[dict[str, Any]] = []
        for item in sorted(self._base(glob)):
            if not item.is_file() or any(p in IGNORED_DIRS for p in item.parts):
                continue
            if item.stat().st_size > 2_000_000:
                continue
            try:
                text = item.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(
                        {
                            "path": self._display(item),
                            "line": number,
                            "text": line.strip()[:300],
                        }
                    )
                    if len(matches) >= limit:
                        return ToolResult.successful(
                            self.name, {"matches": matches, "count": len(matches)}
                        )
        return ToolResult.successful(
            self.name, {"matches": matches, "count": len(matches)}
        )


def build_filesystem_tools(root: Path | None = None) -> list[Tool]:
    """Build the filesystem tools.

    Pass a `root` to confine every path beneath it. Pass `None` (the default)
    for unrestricted access to the whole filesystem, which is the intended
    configuration for a personal agent running as you.
    """
    return [
        ReadTool(root),
        WriteTool(root),
        EditTool(root),
        LsTool(root),
        GlobTool(root),
        GrepTool(root),
    ]
