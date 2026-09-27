"""The AEGIS-X agent loop.

This is the subsystem that did not exist in AEGISAI. `agents/agent.py` called
the provider once and returned; there was no iteration, no tool dispatch, no
observation handling and no termination control. This module adds the missing
cycle:

    ORIENT -> SELECT -> PROMPT -> GENERATE -> DISPATCH -> OBSERVE
           -> (repeat)  |  TERMINATE

Every continuation is recorded as a typed reason (`Continue`) and every
termination as a typed reason (`Terminal`), so a caller can assert *which* path
fired without inspecting message content.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inference.chat import OllamaChat, ToolCall
from tools.base import ToolResult
from tools.registry import ToolRegistry

MAX_TOOL_RESULT_CHARS = 8_000


class Continue:
    """Typed reasons the loop kept going. Mirrors the reference `Continue` union."""

    TOOL_USE = "tool_use"
    TOOL_RETRY = "tool_retry"
    CONTEXT_COMPACT = "context_compact"
    MAX_STEP_CONTINUE = "max_step_continue"

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail

    def __repr__(self) -> str:
        return f"Continue({self.reason!r})"


class Terminal:
    """Typed reasons the loop stopped."""

    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    PROVIDER_ERROR = "provider_error"
    TOOL_LOOP = "tool_loop"
    CANCELLED = "cancelled"

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail

    def __repr__(self) -> str:
        return f"Terminal({self.reason!r})"


@dataclass(slots=True)
class Step:
    index: int
    continuation: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    observation_chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0


@dataclass(slots=True)
class RunResult:
    run_id: str
    goal: str
    answer: str
    terminal: Terminal
    steps: list[Step] = field(default_factory=list)
    duration_s: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.terminal.reason == Terminal.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "answer": self.answer,
            "terminal": {
                "reason": self.terminal.reason,
                "detail": self.terminal.detail,
            },
            "steps": [
                {
                    "index": s.index,
                    "continuation": s.continuation,
                    "tool_calls": s.tool_calls,
                    "observation_chars": s.observation_chars,
                    "prompt_tokens": s.prompt_tokens,
                    "completion_tokens": s.completion_tokens,
                    "latency_ms": s.latency_ms,
                }
                for s in self.steps
            ],
            "duration_s": round(self.duration_s, 3),
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
        }


DEFAULT_SYSTEM = """You are AEGIS-X, an autonomous engineering agent with full access to this machine.

Operating rules:
1. Investigate before you act. Read the files that matter before editing them.
2. Use tools to gather facts. Never guess a file's contents.
3. When you write code, verify it by running it. Prefer run_python or run_shell.
4. Be careful with destructive commands. You can reach every file the user can.
5. When you have enough information, stop calling tools and give a direct final answer.
6. Your final message is the deliverable. Keep it short and concrete.

Available tools are listed in the request. Call one when you need new information or need to change something on disk."""


class AgentLoop:
    def __init__(
        self,
        client: OllamaChat,
        registry: ToolRegistry,
        *,
        system: str = DEFAULT_SYSTEM,
        context: str = "",
        max_steps: int = 12,
        max_tool_repeats: int = 3,
        log_path: Path | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.client = client
        self.registry = registry
        self.system = system
        self.context = context
        self.max_steps = max_steps
        self.max_tool_repeats = max_tool_repeats
        self.log_path = log_path
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------- public API

    def run(self, goal: str) -> RunResult:
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal must be a non-empty string")
        run_id = str(uuid.uuid4())
        started = time.perf_counter()
        # The volatile context rides in the *user* turn, not the system turn.
        #
        # Ollama caches the longest common prefix of a request, and it renders
        # /api/chat as: system, then tools, then the conversation. Anything
        # volatile in the system turn therefore invalidates the cache for the
        # 800-token tool block too, and the run pays full price for it. Keeping
        # the system turn static and the volatile material after the tools
        # means only the tail is reprocessed.
        first_turn = f"{self.context}\n\n{goal}" if self.context else goal
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": first_turn},
        ]
        schemas = self._schemas()
        steps: list[Step] = []
        total_prompt = total_completion = 0
        repeats: dict[str, int] = {}
        answer = ""
        terminal = Terminal(Terminal.MAX_STEPS)

        for index in range(1, self.max_steps + 1):
            try:
                result = self.client.chat(messages, tools=schemas or None)
            except Exception as exc:  # noqa: BLE001 - provider boundary, any failure terminates the run
                terminal = Terminal(Terminal.PROVIDER_ERROR, str(exc))
                break

            total_prompt += result.prompt_tokens
            total_completion += result.completion_tokens
            assistant: dict[str, Any] = {"role": "assistant", "content": result.content}
            if result.tool_calls:
                assistant["tool_calls"] = [
                    {"function": {"name": c.name, "arguments": c.arguments}}
                    for c in result.tool_calls
                ]
            messages.append(assistant)

            if not result.wants_tool:
                answer = result.content.strip()
                terminal = Terminal(Terminal.COMPLETED)
                steps.append(
                    Step(
                        index=index,
                        continuation="none",
                        prompt_tokens=result.prompt_tokens,
                        completion_tokens=result.completion_tokens,
                        latency_ms=result.latency_ms,
                    )
                )
                break

            step = Step(
                index=index,
                continuation=Continue.TOOL_USE,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=result.latency_ms,
            )
            looped = False
            for call in result.tool_calls:
                repeats[call.name] = repeats.get(call.name, 0) + 1
                if repeats[call.name] > self.max_tool_repeats:
                    looped = True
                    observation = (
                        f"error: tool '{call.name}' has been called {repeats[call.name]} times. "
                        "Change approach or answer now."
                    )
                else:
                    observation = self._invoke(call)
                step.tool_calls.append(
                    {
                        "name": call.name,
                        "arguments": call.arguments,
                        "chars": len(observation),
                    }
                )
                step.observation_chars += len(observation)
                messages.append(
                    {"role": "tool", "name": call.name, "content": observation}
                )
            steps.append(step)

            if looped:
                terminal = Terminal(
                    Terminal.TOOL_LOOP, "a tool repeated too many times"
                )
                break
            if index == self.max_steps:
                answer = (messages[-1].get("content") or "").strip()
                terminal = Terminal(
                    Terminal.MAX_STEPS, f"exceeded {self.max_steps} steps"
                )
        else:
            answer = (messages[-1].get("content") or "").strip()

        outcome = RunResult(
            run_id=run_id,
            goal=goal,
            answer=answer,
            terminal=terminal,
            steps=steps,
            duration_s=time.perf_counter() - started,
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
        )
        self._persist(outcome, messages)
        return outcome

    # --------------------------------------------------------------- internal

    def _invoke(self, call: ToolCall) -> str:
        try:
            result: ToolResult = self.registry.execute(call.name, call.arguments)
        except Exception as exc:  # noqa: BLE001 - tool boundary, any failure becomes an observation
            return f"error: {type(exc).__name__}: {exc}"
        if result.success:
            payload = result.output
        else:
            payload = {"error": result.error or "tool failed"}
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) > MAX_TOOL_RESULT_CHARS:
            text = (
                text[:MAX_TOOL_RESULT_CHARS]
                + f"\n... [truncated {len(text) - MAX_TOOL_RESULT_CHARS} chars]"
            )
        return text

    def _schemas(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for tool in self.registry.list_tools():
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
            )
        return schemas

    def _persist(
        self, result: RunResult, messages: Sequence[Mapping[str, Any]]
    ) -> None:
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging must never break a run
