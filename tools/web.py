"""Live web tools the agent can call: search and fetch.

These are the missing piece. The harvest worker fetches URLs in bulk, but
until now the agent itself had no way to reach the network at query time, so
"connected to the web" meant a batch job someone else started. These two tools
make it a capability the agent exercises itself.

Both require `Capability.NETWORK`, so the existing `PermissionPolicy` gates
them. Neither writes to disk. Fetching reuses `memory.ingest`, which means
robots.txt, the content-type allowlist, and the byte ceiling are already
enforced rather than reimplemented.

    from tools.web import build_web_tools
    from tools.registry import ToolRegistry
    from tools.permissions import Capability, PermissionPolicy

    registry = ToolRegistry()
    for tool in build_web_tools(treasury=bank):
        registry.register(tool)

    policy = PermissionPolicy([Capability.READ_ONLY, Capability.NETWORK])
    registry.execute("web_search", {"query": "sqlite wal mode"}, policy=policy)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from memory.errors import (
    BudgetExhaustedError,
    DocumentTooLargeError,
    IngestError,
    RobotsDisallowedError,
)
from memory.ingest import WebIngestor
from tools.base import Tool, ToolResult
from tools.permissions import Capability
from tools.search import (
    SearchCredentialsMissingError,
    SearchError,
    build_search_provider,
    describe_providers,
)

MAX_SNIPPET_CHARS = 6000


class _WebTool(Tool):
    """Shared plumbing: a consistent error shape across network failures."""

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: Mapping[str, Any],
        output_schema: Mapping[str, Any],
        *,
        cost_per_request: float = 0.0,
    ) -> None:
        super().__init__(
            name,
            description,
            input_schema,
            output_schema,
            Capability.NETWORK,
        )
        self.cost_per_request = cost_per_request

    def _failure_output(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """A schema-valid output for a failed call. Subclasses override."""
        return {}

    def _fail(
        self, arguments: Mapping[str, Any], kind: str, message: str
    ) -> ToolResult:
        """Report a failure with output that still satisfies the schema.

        `ToolResult.failed` returns an empty output mapping, but
        `ToolRegistry.execute` validates output against `output_schema` for
        every result including failures. An empty mapping therefore fails
        validation and the registry raises `InvalidToolOutputError`, which
        hides the real reason entirely. Emitting a schema-shaped payload with
        `success=False` and the error attached keeps the actual cause visible.
        """
        payload = self._failure_output(arguments)
        payload["error"] = message
        payload["error_kind"] = kind
        return ToolResult(self.name, payload, False, message, {"kind": kind})

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        try:
            return self._run(arguments)
        except SearchCredentialsMissingError as exc:
            return self._fail(arguments, "credentials", str(exc))
        except SearchError as exc:
            return self._fail(arguments, "search", str(exc))
        except RobotsDisallowedError as exc:
            return self._fail(arguments, "robots", str(exc))
        except BudgetExhaustedError as exc:
            return self._fail(arguments, "budget", str(exc))
        except DocumentTooLargeError as exc:
            return self._fail(arguments, "too_large", str(exc))
        except IngestError as exc:
            return self._fail(arguments, "network", str(exc))
        except (ValueError, TypeError) as exc:
            return self._fail(arguments, "input", str(exc))

    def _run(self, arguments: Mapping[str, Any]) -> ToolResult:
        raise NotImplementedError


class WebSearchTool(_WebTool):
    """Search the web and return titles, URLs, and snippets."""

    def __init__(
        self,
        *,
        provider: str | None = None,
        timeout: float = 15.0,
        cost_per_request: float = 0.0,
    ) -> None:
        super().__init__(
            "web_search",
            (
                "Search the web for current information. Returns a list of "
                "results with title, url, and a text snippet. Use this when "
                "the answer depends on facts you cannot derive, and prefer "
                "web_fetch to read a specific known page."
            ),
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                        "minLength": 2,
                        "maxLength": 512,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "How many results to return.",
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "provider": {"type": "string"},
                    "count": {"type": "integer"},
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "url": {"type": "string"},
                                "snippet": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["query", "results"],
            },
            cost_per_request=cost_per_request,
        )
        self.provider = build_search_provider(provider, timeout=timeout)

    def _failure_output(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "query": str(arguments.get("query", "")),
            "provider": self.provider.name,
            "count": 0,
            "results": [],
        }

    def _run(self, arguments: Mapping[str, Any]) -> ToolResult:
        query = str(arguments["query"]).strip()
        limit = int(arguments.get("limit", 5))
        hits = self.provider.search(query, limit=limit)
        return ToolResult.successful(
            self.name,
            {
                "query": query,
                "provider": self.provider.name,
                "count": len(hits),
                "results": [hit.to_dict() for hit in hits],
            },
            {"providers": describe_providers()},
        )


class WebFetchTool(_WebTool):
    """Fetch one URL and return its readable text."""

    def __init__(
        self,
        *,
        ingestor: WebIngestor,
        max_chars: int = MAX_SNIPPET_CHARS,
        cost_per_request: float = 0.0,
    ) -> None:
        super().__init__(
            "web_fetch",
            (
                "Fetch a web page over http or https and return its readable "
                "text content. Respects robots.txt. Use this to read a specific "
                "page, such as a documentation URL or an API reference."
            ),
            {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute http or https URL.",
                        "minLength": 8,
                        "maxLength": 2048,
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Truncate the returned text.",
                        "minimum": 200,
                        "maximum": 50000,
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "content_type": {"type": "string"},
                    "characters": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                    "text": {"type": "string"},
                },
                "required": ["url", "text"],
            },
            cost_per_request=cost_per_request,
        )
        self.ingestor = ingestor
        self.max_chars = max(200, max_chars)

    def _failure_output(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "url": str(arguments.get("url", "")),
            "title": "",
            "content_type": "",
            "characters": 0,
            "truncated": False,
            "text": "",
        }

    def _run(self, arguments: Mapping[str, Any]) -> ToolResult:
        url = str(arguments["url"]).strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        limit = min(int(arguments.get("max_chars", self.max_chars)), self.max_chars)
        document = self.ingestor.fetch(url)
        text = document.text
        truncated = len(text) > limit
        if truncated:
            text = text[:limit]
        return ToolResult.successful(
            self.name,
            {
                "url": document.uri,
                "title": document.title,
                "content_type": document.content_type,
                "characters": len(text),
                "truncated": truncated,
                "text": text,
            },
            {"fetched_at": document.fetched_at},
        )


def build_web_tools(
    *,
    treasury: Any = None,
    index: Any = None,
    provider: str | None = None,
    delay: float = 1.0,
    timeout: float = 15.0,
    cost_per_request: float = 0.0,
    max_chars: int = MAX_SNIPPET_CHARS,
) -> list[Tool]:
    """Build the web tools, sharing one metered ingestor.

    The ingestor is shared deliberately: it owns the robots.txt cache and the
    politeness delay, so two tools cannot between them hammer a host.
    """
    if index is None:
        from memory.store import MemoryIndex

        index = MemoryIndex(":memory:")
    ingestor = WebIngestor(index, treasury=treasury, delay=delay, timeout=timeout)
    ingestor.cost_per_fetch = cost_per_request
    return [
        WebSearchTool(provider=provider, timeout=timeout),
        WebFetchTool(
            ingestor=ingestor, max_chars=max_chars, cost_per_request=cost_per_request
        ),
    ]
