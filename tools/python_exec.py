"""Sandboxed Python and shell execution.

Replaces the hard `PROCESS_EXECUTION` prohibition with a *sandbox-gated*
capability: grantable, but every invocation runs with a wall-clock timeout, a
capped output buffer, no inherited stdin, and a scrubbed environment. It is not
a kernel-level jail (see docs/development.md) but it fails closed on runaway
output and hanging processes, which is what actually breaks agents in practice.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools.base import Tool, ToolResult

SANDBOXED_EXECUTION = "SANDBOXED_EXECUTION"
MAX_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0
SCRUBBED_ENV = {
    "AWS_",
    "AZURE_",
    "GCP_",
    "GOOGLE_",
    "OPENAI_",
    "ANTHROPIC_",
    "HF_",
    "HUGGINGFACE_",
    "NPM_",
    "DOCKER_",
    "KUBECONFIG",
    "SSH_",
    "STRIPE_",
    "BRAVE_",
    "TAVILY_",
    "SERPER_",
}


def scrubbed_env() -> dict[str, str]:
    """A child environment with credential-shaped variables removed.

    Both execution tools use this. An earlier version applied it only to the
    Python runner, which left the shell runner inheriting the parent's full
    environment - so any command the model asked for could read every API key
    the user had set.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(k.upper().startswith(prefix) for prefix in SCRUBBED_ENV)
    }
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class PythonExecTool(Tool):
    def __init__(self, root: Path, timeout: float = DEFAULT_TIMEOUT) -> None:
        super().__init__(
            "run_python",
            "Execute a short Python script inside the workspace and return stdout, stderr and the exit code. Use for computation, verification and quick scripts.",
            {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "minLength": 1, "maxLength": 200000},
                    "timeout": {"type": "number", "minimum": 1, "maximum": MAX_TIMEOUT},
                },
                "required": ["code"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "duration_s": {"type": "number"},
                },
                "required": ["stdout", "stderr", "exit_code"],
                "additionalProperties": False,
            },
            SANDBOXED_EXECUTION,  # type: ignore[arg-type]
        )
        self.root = root.resolve()
        self.timeout = timeout

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        code = arguments["code"]
        if not isinstance(code, str) or not code.strip():
            return ToolResult.failed(self.name, "code must be a non-empty string")
        timeout = min(float(arguments.get("timeout", self.timeout)), MAX_TIMEOUT)
        script = self.root / ".aegis_exec.py"
        try:
            script.write_text(code, encoding="utf-8")
            started = time.perf_counter()
            # fixed argv, no shell
            completed = subprocess.run(
                [sys.executable, "-I", str(script)],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._env(),
                stdin=subprocess.DEVNULL,
                check=False,
            )
            duration = round(time.perf_counter() - started, 3)
        except subprocess.TimeoutExpired:
            return ToolResult.failed(
                self.name,
                f"execution exceeded {timeout:.0f}s and was killed",
                {"duration_s": timeout},
            )
        except OSError as exc:
            return ToolResult.failed(self.name, f"could not start interpreter: {exc}")
        finally:
            script.unlink(missing_ok=True)
        return ToolResult.successful(
            self.name,
            {
                "stdout": self._cap(completed.stdout),
                "stderr": self._cap(completed.stderr),
                "exit_code": int(completed.returncode),
                "duration_s": duration,
            },
        )

    def _env(self) -> dict[str, str]:
        return scrubbed_env()

    @staticmethod
    def _cap(text: str) -> str:
        if text is None:
            return ""
        if len(text) <= MAX_OUTPUT_CHARS:
            return text
        return (
            text[:MAX_OUTPUT_CHARS]
            + f"\n... [truncated {len(text) - MAX_OUTPUT_CHARS} chars]"
        )


class ShellTool(Tool):
    def __init__(self, root: Path, timeout: float = DEFAULT_TIMEOUT) -> None:
        super().__init__(
            "run_shell",
            "Run one shell command in the workspace. Use for git, pip, pytest, build and other CLI work. Prefer run_python for pure computation.",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "minLength": 1, "maxLength": 8000},
                    "timeout": {"type": "number", "minimum": 1, "maximum": MAX_TIMEOUT},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": "integer"},
                },
                "required": ["stdout", "exit_code"],
                "additionalProperties": False,
            },
            SANDBOXED_EXECUTION,  # type: ignore[arg-type]
        )
        self.root = root.resolve()
        self.timeout = timeout

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        command = arguments["command"]
        if not isinstance(command, str) or not command.strip():
            return ToolResult.failed(self.name, "command must be a non-empty string")
        timeout = min(float(arguments.get("timeout", self.timeout)), MAX_TIMEOUT)
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.root),
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=scrubbed_env(),
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failed(
                self.name, f"command exceeded {timeout:.0f}s and was killed"
            )
        except OSError as exc:
            return ToolResult.failed(self.name, f"could not start shell: {exc}")
        return ToolResult.successful(
            self.name,
            {
                "stdout": PythonExecTool._cap(completed.stdout),
                "stderr": PythonExecTool._cap(completed.stderr),
                "exit_code": int(completed.returncode),
            },
        )


def build_exec_tools(root: Path, timeout: float = DEFAULT_TIMEOUT) -> list[Tool]:
    return [PythonExecTool(root, timeout), ShellTool(root, timeout)]
