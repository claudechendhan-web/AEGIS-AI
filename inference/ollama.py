from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.config import Settings
from core.errors import ProviderConnectionError, ProviderResponseError
from core.models import AgentRequest, AgentResponse, Message, MessageRole
from inference.base import InferenceProvider, ProviderHealth


class OllamaProvider(InferenceProvider):
    """Synchronous client for the local Ollama HTTP API."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model_name: str | None = None,
        timeout: float = 120.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings or Settings.from_environment()
        self.model_name = model_name or self.settings.model_name
        if not isinstance(self.model_name, str) or not self.model_name.strip():
            raise ValueError("model_name must be a non-empty string")
        self.model_name = self.model_name.strip()
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.timeout = timeout
        self._opener = opener or urlopen

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def base_url(self) -> str:
        return self.settings.ollama_base_url

    def generate(self, request: AgentRequest) -> AgentResponse:
        if not isinstance(request, AgentRequest):
            raise TypeError("request must be an AgentRequest")
        payload = {
            "model": self.model_name,
            "prompt": self._build_prompt(request),
            "stream": False,
        }
        response_data = self._send_json("POST", "/api/generate", payload)
        content = response_data.get("response")
        if not isinstance(content, str):
            raise ProviderResponseError(
                "Ollama response did not contain a text response"
            )
        conversation = request.conversation
        if conversation is not None:
            conversation.add_message(
                Message(
                    role=MessageRole.ASSISTANT,
                    content=content,
                    metadata={"provider": self.name, "model": self.model_name},
                )
            )
        return AgentResponse(
            content=content,
            request_id=request.request_id,
            provider=self.name,
            model=self.model_name,
            conversation=conversation,
            metadata={"done": response_data.get("done", True)},
        )

    def _build_prompt(self, request: AgentRequest) -> str:
        if request.conversation is None:
            return request.prompt
        messages = list(request.conversation.messages)
        if (
            len(messages) == 1
            and messages[0].role is MessageRole.USER
            and messages[0].content == request.prompt
        ):
            return request.prompt
        if not messages:
            return request.prompt
        if not (
            messages[-1].role is MessageRole.USER
            and messages[-1].content == request.prompt
        ):
            messages.append(Message(role=MessageRole.USER, content=request.prompt))
        history = "\n".join(
            f"{MessageRole(message.role).value}: {message.content}"
            for message in messages
        )
        return f"Conversation history:\n{history}"

    def health_check(self) -> ProviderHealth:
        started = time.perf_counter()
        reachable = False
        detail = ""
        try:
            response_data = self._send_json("GET", "/api/tags")
            reachable = True
            detail = f"Ollama server is reachable at {self.base_url}"
            models = response_data.get("models")
            if isinstance(models, list):
                names = {
                    item.get("name")
                    for item in models
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                }
                if self.model_name not in names:
                    detail += f"; configured model '{self.model_name}' was not listed"
        except (
            ProviderConnectionError,
            ProviderResponseError,
            ValueError,
            TypeError,
        ) as exc:
            detail = f"Ollama server is unavailable: {exc}"
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return ProviderHealth(
            provider=self.name,
            model=self.model_name,
            reachable=reachable,
            detail=detail,
            latency_ms=latency_ms,
        )

    def _send_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        http_request = Request(url, data=data, headers=headers, method=method)
        response = None
        try:
            response = self._opener(http_request, timeout=self.timeout)
            status = getattr(response, "status", 200)
            if isinstance(status, int) and status >= 400:
                raise ProviderResponseError(f"Ollama returned HTTP {status}")
            raw_data = response.read()
        except HTTPError as exc:
            raise ProviderResponseError(f"Ollama returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderConnectionError(
                f"unable to connect to Ollama at {self.base_url}: {exc}"
            ) from exc
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if close is not None:
                    close()
        try:
            if isinstance(raw_data, bytes):
                raw_text = raw_data.decode("utf-8")
            elif isinstance(raw_data, str):
                raw_text = raw_data
            else:
                raise ProviderResponseError(
                    "Ollama returned an unsupported response body"
                )
            decoded = json.loads(raw_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderResponseError("Ollama returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ProviderResponseError("Ollama returned a non-object JSON response")
        return decoded
