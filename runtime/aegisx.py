"""AEGIS-X runtime: assembles the model, the tools and the loop.

`runtime/runtime.py` (Phase 1) is preserved untouched. This is the new
composition root for the autonomous agent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.loop import DEFAULT_SYSTEM, AgentLoop, RunResult
from inference.chat import OllamaChat
from inference.options import ModelOptions
from tools.calculator import CalculatorTool
from tools.filesystem import build_filesystem_tools
from tools.permissions import Capability, PermissionPolicy
from tools.python_exec import build_exec_tools
from tools.registry import ToolRegistry

DEFAULT_WORKSPACE = Path("workspace")
DEFAULT_RUN_LOG = Path("data/runs.jsonl")

# The agent needs more than READ_ONLY to do any real work. PROCESS_EXECUTION
# stays permanently denied; SANDBOXED_EXECUTION is the grantable replacement.
DEFAULT_CAPABILITIES = (
    Capability.READ_ONLY,
    Capability.FILESYSTEM_READ,
    Capability.FILESYSTEM_WRITE,
    Capability.SANDBOXED_EXECUTION,
)

SYSTEM_APPENDIX = """
Work only inside the provided workspace. Prefer small, verifiable steps.
When you finish, reply with the result and nothing else - no preamble."""


@dataclass(slots=True)
class AEGISX:
    loop: AgentLoop
    registry: ToolRegistry
    client: OllamaChat
    workspace: Path

    def run(self, goal: str) -> RunResult:
        return self.loop.run(goal)

    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.registry.list_tools()]


def build(
    workspace: Path | str = DEFAULT_WORKSPACE,
    *,
    model: str = "qwen2.5-coder:3b",
    base_url: str = "http://127.0.0.1:11434",
    max_steps: int = 12,
    timeout: float = 60.0,
    options: ModelOptions | None = None,
    run_log: Path | str | None = DEFAULT_RUN_LOG,
    extra_tools: list[Any] | None = None,
    capabilities: tuple[Capability, ...] = DEFAULT_CAPABILITIES,
    confine_workspace: bool = False,
) -> AEGISX:
    """Assemble the model, tools and loop.

    Unrestricted by default: the filesystem tools reach any path you can.
    Pass `confine_workspace=True` to restrict them to `workspace`.
    """
    root = Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    policy = PermissionPolicy(capabilities)
    registry = ToolRegistry(policy)
    for tool in [
        CalculatorTool(),
        *build_filesystem_tools(root if confine_workspace else None),
        *build_exec_tools(root, timeout),
    ]:
        registry.register(tool)
    for tool in extra_tools or []:
        registry.register(tool)

    client = OllamaChat(
        model=model,
        base_url=base_url,
        options=options
        or ModelOptions(temperature=0.2, num_ctx=8192, num_predict=1024),
        timeout=max(timeout * 4, 120.0),
    )
    loop = AgentLoop(
        client=client,
        registry=registry,
        system=DEFAULT_SYSTEM + "\n" + SYSTEM_APPENDIX,
        max_steps=max_steps,
        log_path=Path(run_log) if run_log else None,
    )
    return AEGISX(loop=loop, registry=registry, client=client, workspace=root)


def health(
    base_url: str = "http://127.0.0.1:11434", model: str = "qwen2.5-coder:7b"
) -> dict[str, Any]:
    try:
        return OllamaChat(model=model, base_url=base_url).health()
    except Exception as exc:  # noqa: BLE001 - health probe, any failure means unreachable
        return {"reachable": False, "error": str(exc), "model": model}


def render(result: RunResult) -> str:
    return json.dumps(result.to_dict(), indent=2, ensure_ascii=False)
