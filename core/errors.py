class AegisError(Exception):
    """Base exception for AEGISAI errors."""


class ConfigurationError(AegisError):
    """Raised when configuration is missing or invalid."""


class InferenceFailure(AegisError):
    """Base exception for inference failures."""


class ProviderError(InferenceFailure):
    """Base exception for inference provider failures."""


class ProviderConnectionError(ProviderError):
    """Raised when an inference provider cannot be reached."""


class ProviderResponseError(ProviderError):
    """Raised when an inference provider returns an invalid response."""


class AgentExecutionError(AegisError):
    """Raised when an agent cannot complete a request."""


class DatabaseError(AegisError):
    """Raised when persistent storage cannot complete an operation."""


class ConversationNotFoundError(AegisError):
    """Raised when a requested conversation does not exist."""


class InvalidMessageRoleError(AegisError, ValueError):
    """Raised when a message has an unsupported role."""


class DuplicateToolError(AegisError):
    """Raised when registering a tool name more than once."""


class UnknownToolError(AegisError):
    """Raised when a requested tool is not registered."""


class PermissionDeniedError(AegisError):
    """Raised when a capability is not permitted."""


class InvalidToolInputError(AegisError, ValueError):
    """Raised when tool input does not match its schema."""


class InvalidToolOutputError(AegisError):
    """Raised when a tool returns output that does not match its schema."""
