from agents.agent import Agent
from core.models import AgentRequest, AgentResponse, Conversation
from inference.base import InferenceProvider, ProviderHealth
from tools.base import ToolCall
from tools.calculator import CalculatorTool
from tools.registry import ToolRegistry


class RecordingProvider(InferenceProvider):
    @property
    def name(self) -> str:
        return "agent-test"

    def generate(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            content="agent response",
            request_id=request.request_id,
            provider=self.name,
            model="agent-test-model",
        )

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(self.name, "agent-test-model", True)


def test_agent_delegates_request_to_provider() -> None:
    provider = RecordingProvider()
    agent = Agent(provider, name="test-agent")
    conversation = Conversation()
    request = AgentRequest(prompt="Hello", conversation=conversation)

    response = agent.run(request)

    assert response.content == "agent response"
    assert response.conversation is conversation


def test_agent_exposes_explicit_tool_execution_interface() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    agent = Agent(RecordingProvider(), tool_registry=registry)

    result = agent.execute_tool(
        ToolCall(name="calculator", arguments={"expression": "2 + 3"})
    )

    assert result.output == {"result": 5}
