import inspect

from core.errors import ProviderConnectionError
from core.models import AgentRequest, AgentResponse
from inference.base import InferenceProvider, ProviderHealth


class StubProvider(InferenceProvider):
    @property
    def name(self) -> str:
        return "stub"

    def generate(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            content="stub response",
            request_id=request.request_id,
            provider=self.name,
            model="stub-model",
            conversation=request.conversation,
        )

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(self.name, "stub-model", True, "stub available", 0.1)


class BrokenProvider(StubProvider):
    def generate(self, request: AgentRequest) -> AgentResponse:
        raise ProviderConnectionError("stub unavailable")


def test_provider_contract_supports_generate_prompt() -> None:
    provider = StubProvider()

    response = provider.generate_prompt("Hello")

    assert isinstance(response, AgentResponse)
    assert response.content == "stub response"
    assert response.request_id


def test_provider_health_has_healthy_alias() -> None:
    health = StubProvider().health_check()

    assert health.healthy is True
    assert health.to_dict()["reachable"] is True


def test_inference_provider_cannot_be_instantiated_without_contract() -> None:
    assert inspect.isabstract(InferenceProvider)
