"""Model sampling options.

AEGISAI never sent any sampling controls to the provider, so every generation
used Ollama's undocumented defaults. This is the options object the chat
provider forwards.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelOptions:
    temperature: float = 0.2
    top_p: float = 0.9
    top_k: int = 40
    num_ctx: int = 8192
    num_predict: int = 1024
    repeat_penalty: float = 1.1
    stop: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.temperature <= 0:
            raise ValueError("temperature must be greater than zero")
        if not 0 < self.top_p <= 1:
            raise ValueError("top_p must be in (0, 1]")
        if self.top_k <= 0:
            raise ValueError("top_k must be greater than zero")
        if self.num_ctx <= 0:
            raise ValueError("num_ctx must be greater than zero")
        if self.num_predict < 0:
            raise ValueError("num_predict must be zero or greater")
        if self.repeat_penalty <= 0:
            raise ValueError("repeat_penalty must be greater than zero")

    def with_(self, **changes: Any) -> ModelOptions:
        return replace(self, **changes)

    def to_ollama(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
            "repeat_penalty": self.repeat_penalty,
        }
        if self.stop:
            payload["stop"] = list(self.stop)
        return payload


DEFAULT_OPTIONS = ModelOptions()
DETERMINISTIC = ModelOptions(temperature=0.01, top_k=1, repeat_penalty=1.0)
