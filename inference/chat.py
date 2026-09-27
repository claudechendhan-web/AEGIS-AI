"""Ollama /api/chat provider: native messages, native tools, real options.

This is the fix for the single most damaging defect in AEGISAI: the existing
`inference/ollama.py` calls `/api/generate` and flattens the whole conversation
into one string, so the model sees no chat template, no role boundaries and no
tool protocol. `/api/chat` gives us all three.

Standard library only, matching the rest of the project.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.errors import ProviderConnectionError, ProviderResponseError
from inference.options import ModelOptions


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": dict(self.arguments),
            "call_id": self.call_id,
        }


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_calls)


class OllamaChat:
    """Synchronous /api/chat client with tool calling and streaming."""

    def __init__(
        self,
        model: str = "qwen2.5-coder:3b",
        base_url: str = "http://127.0.0.1:11434",
        options: ModelOptions | None = None,
        timeout: float = 300.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.options = options or ModelOptions()
        self.timeout = timeout
        self._opener = opener or urlopen

    # ---------------------------------------------------------------- public

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        options: ModelOptions | None = None,
    ) -> ChatResult:
        body = self._body(messages, tools, options)
        data = self._post("/api/chat", body)
        return self._to_result(data)

    def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        options: ModelOptions | None = None,
    ) -> Iterator[ChatResult]:
        """Yield incremental deltas. The final yield carries finish_reason."""
        body = dict(self._body(messages, tools, options))
        body["stream"] = True
        started = time.perf_counter()
        content_parts: list[str] = []
        pending: dict[int, dict[str, Any]] = {}
        finish = ""
        for line in self._post_lines("/api/chat", body):
            if not line.strip():
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProviderResponseError(
                    "Ollama stream returned invalid JSON"
                ) from exc
            message = chunk.get("message") or {}
            piece = message.get("content")
            if isinstance(piece, str) and piece:
                content_parts.append(piece)
            for call in message.get("tool_calls") or []:
                self._accumulate(pending, call)
            if chunk.get("done"):
                finish = str(chunk.get("done_reason") or "stop")
        yield ChatResult(
            content="".join(content_parts),
            tool_calls=tuple(self._finalize(pending)),
            finish_reason=finish,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )

    def health(self) -> dict[str, Any]:
        data = self._get("/api/tags")
        names = [
            item.get("name")
            for item in (data.get("models") or [])
            if isinstance(item, Mapping)
        ]
        return {
            "reachable": True,
            "model": self.model,
            "model_present": self.model in names,
            "available_models": [n for n in names if isinstance(n, str)],
        }

    # --------------------------------------------------------------- private

    def _body(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
        options: ModelOptions | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(m) for m in messages],
            "stream": False,
            # Hold the model resident after the response. Ollama's default is
            # 5 minutes, and when the model unloads it discards the prompt
            # cache with it - so the next run pays full price to re-read a
            # prompt that has not changed. Measured on CPU-only hardware, an
            # unchanged prompt costs 61s cold versus 4s warm, and across a
            # multi-step run that difference is paid once per invocation.
            # Long enough to cover an editing session, short enough that an
            # idle laptop is not left holding 2GB of resident weights.
            "keep_alive": "30m",
            "options": (options or self.options).to_ollama(),
        }
        if tools:
            body["tools"] = [dict(t) for t in tools]
        return body

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        for line in self._post_lines(path, body):
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProviderResponseError("Ollama returned invalid JSON") from exc
            if not isinstance(decoded, dict):
                raise ProviderResponseError("Ollama returned a non-object response")
            return decoded
        raise ProviderResponseError("Ollama returned an empty response")

    def _post_lines(self, path: str, body: Mapping[str, Any]) -> Iterator[str]:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        response = None
        try:
            response = self._opener(request, timeout=self.timeout)
            status = getattr(response, "status", 200)
            if isinstance(status, int) and status >= 400:
                raise ProviderResponseError(f"Ollama returned HTTP {status}")
            while True:
                raw = response.readline()
                if not raw:
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                yield raw
        except HTTPError as exc:
            raise ProviderResponseError(f"Ollama returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderConnectionError(
                f"unable to reach Ollama at {self.base_url}: {exc}"
            ) from exc
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

    def _get(self, path: str) -> dict[str, Any]:
        request = Request(
            f"{self.base_url}{path}", headers={"Accept": "application/json"}
        )
        response = None
        try:
            response = self._opener(request, timeout=min(self.timeout, 30.0))
            raw = response.read()
        except HTTPError as exc:
            raise ProviderResponseError(f"Ollama returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderConnectionError(
                f"unable to reach Ollama at {self.base_url}: {exc}"
            ) from exc
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError("Ollama returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ProviderResponseError("Ollama returned a non-object response")
        return decoded

    def _to_result(self, data: Mapping[str, Any]) -> ChatResult:
        message = data.get("message")
        if not isinstance(message, Mapping):
            raise ProviderResponseError("Ollama response had no message object")
        content = str(message.get("content") or "")
        pending: dict[int, dict[str, Any]] = {}
        for call in message.get("tool_calls") or []:
            self._accumulate(pending, call)
        calls = self._finalize(pending)
        if not calls and content:
            # Small local models frequently ignore the native `tools` channel and
            # emit the call as raw JSON text instead. Recover it rather than
            # silently treating the turn as a final answer.
            recovered = self._calls_from_text(content)
            if recovered is not None:
                calls = recovered
                content = ""
        return ChatResult(
            content=content,
            tool_calls=tuple(calls),
            finish_reason=str(
                data.get("done_reason") or ("stop" if data.get("done") else "")
            ),
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
            raw=data,
        )

    @staticmethod
    def _calls_from_text(content: str) -> list[ToolCall] | None:
        """Recover tool calls a model printed as text. None means 'not a call'."""
        text = content.strip()
        starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
        if not starts:
            return None
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text[min(starts) :])
        except json.JSONDecodeError:
            return None
        items = parsed if isinstance(parsed, list) else [parsed]
        calls: list[ToolCall] = []
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                return None
            raw_function = item.get("function")
            function = raw_function if isinstance(raw_function, Mapping) else item
            name = function.get("name") or item.get("tool") or item.get("tool_name")
            if not isinstance(name, str) or not name:
                return None
            arguments = function.get(
                "arguments", function.get("parameters", item.get("input"))
            )
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"_raw": arguments}
            if not isinstance(arguments, Mapping):
                arguments = {}
            calls.append(
                ToolCall(name=name, arguments=dict(arguments), call_id=f"text_{index}")
            )
        return calls or None

    @staticmethod
    def _accumulate(pending: dict[int, dict[str, Any]], call: Any) -> None:
        if not isinstance(call, Mapping):
            return
        index = int(call.get("index", len(pending)))
        function = call.get("function")
        if not isinstance(function, Mapping):
            return
        slot = pending.setdefault(index, {"name": "", "arguments": ""})
        name = function.get("name")
        if isinstance(name, str) and name:
            slot["name"] = name
        arguments = function.get("arguments")
        if isinstance(arguments, Mapping):
            slot["arguments"] = json.dumps(dict(arguments), ensure_ascii=False)
        elif isinstance(arguments, str):
            slot["arguments"] += arguments

    @staticmethod
    def _finalize(pending: Mapping[int, Mapping[str, Any]]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index in sorted(pending):
            slot = pending[index]
            raw = slot.get("arguments") or "{}"
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                parsed = {"_raw": raw}
            if not isinstance(parsed, dict):
                parsed = {"_raw": parsed}
            calls.append(
                ToolCall(
                    name=str(slot.get("name") or ""),
                    arguments=parsed,
                    call_id=f"call_{index}",
                )
            )
        return calls
