from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from core.models import AgentRequest, AgentResponse


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider: str
    model: str
    reachable: bool
    detail: str = ""
    latency_ms: float | None = None

    @property
    def healthy(self) -> bool:
        return self.reachable

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "reachable": self.reachable,
            "healthy": self.healthy,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }


class InferenceProvider(ABC):
    """Provider contract for synchronous text generation."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def generate(self, request: AgentRequest) -> AgentResponse:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> ProviderHealth:
        raise NotImplementedError

    def generate_prompt(self, prompt: str) -> AgentResponse:
        return self.generate(AgentRequest(prompt=prompt))

    def close(self) -> None:
        return None
