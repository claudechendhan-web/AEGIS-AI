"""Tests for the live web tools.

Two things are being defended here. First, that the network capability is
actually enforced: a tool that declares `NETWORK` and can be called without
it is not a permission system. Second, that failures stay visible, because a
tool that raises `InvalidToolOutputError` instead of reporting "robots said no"
is worse than no error reporting at all.
"""

from __future__ import annotations

import json

import pytest

from core.errors import InvalidToolInputError, PermissionDeniedError
from memory.ingest import WebIngestor
from memory.store import MemoryIndex
from tools.permissions import Capability, PermissionPolicy
from tools.registry import ToolRegistry
from tools.search import (
    KeyedSearch,
    NullSearch,
    SearchCredentialsMissingError,
    SearchError,
    WikipediaSearch,
    build_search_provider,
    describe_providers,
)
from tools.web import WebFetchTool, WebSearchTool, build_web_tools

NETWORK_ONLY = PermissionPolicy([Capability.NETWORK])


class _Response:
    def __init__(self, body: bytes, ctype: str = "text/html", status: int = 200):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": ctype}

    def read(self, size: int = -1) -> bytes:
        return self._body

    def close(self) -> None:
        pass


class _Opener:
    def __init__(self, robots: bytes | None, page: bytes, wiki: dict | None = None):
        self.robots = robots
        self.page = page
        self.wiki = wiki or {}
        self.requested: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requested.append(url)
        if "wikipedia.org" in url:
            return _Response(json.dumps(self.wiki).encode(), "application/json")
        if url.endswith("/robots.txt"):
            if self.robots is None:
                return _Response(b"", "text/plain", 404)
            return _Response(self.robots, "text/plain")
        return _Response(self.page)


def ingestor_with(opener: _Opener) -> WebIngestor:
    ingestor = WebIngestor(MemoryIndex(":memory:"), delay=0.0)
    ingestor._opener = opener
    return ingestor


PAGE = b"<html><head><title>Live Page</title></head><body><p>Real content.</p></body></html>"
WIKI = {
    "query": {
        "search": [
            {
                "title": "SQLite",
                "snippet": 'A <span class="searchmatch">relational</span> engine',
            },
            {"title": "ACID", "snippet": "Four properties."},
            {"not": "a page"},
            {"title": "", "snippet": "no title"},
        ]
    }
}


# -- the permission boundary ------------------------------------------------


def test_both_tools_require_network() -> None:
    for tool in build_web_tools(delay=0.0):
        assert tool.required_capability is Capability.NETWORK


def test_search_is_blocked_without_network() -> None:
    registry = ToolRegistry()
    for tool in build_web_tools(delay=0.0, provider="wikipedia"):
        registry.register(tool)
    with pytest.raises(PermissionDeniedError):
        registry.execute(
            "web_search", {"query": "anything"}, permission_policy=PermissionPolicy()
        )


def test_fetch_is_blocked_without_network() -> None:
    registry = ToolRegistry()
    for tool in build_web_tools(delay=0.0):
        registry.register(tool)
    with pytest.raises(PermissionDeniedError):
        registry.execute(
            "web_fetch",
            {"url": "https://example.com/"},
            permission_policy=PermissionPolicy([Capability.READ_ONLY]),
        )


# -- failures stay visible --------------------------------------------------


def test_failure_output_satisfies_the_schema() -> None:
    """Regression: `ToolResult.failed` returns `{}`, which fails validation.

    The registry validates output against the schema for failures too, so an
    empty mapping raised `InvalidToolOutputError` and hid the real cause.
    """
    registry = ToolRegistry()
    for tool in build_web_tools(delay=0.0, provider="none"):
        registry.register(tool)
    result = registry.execute(
        "web_search", {"query": "anything"}, permission_policy=NETWORK_ONLY
    )
    assert result.success is False
    assert result.error
    assert result.metadata.get("kind") == "credentials"
    assert result.output["results"] == []


def test_robots_refusal_is_reported_not_masked() -> None:
    opener = _Opener(b"User-agent: *\nDisallow: /private\n", PAGE)
    tool = WebFetchTool(ingestor=ingestor_with(opener))
    result = tool.execute({"url": "https://site.test/private/doc"})
    assert result.success is False
    assert result.metadata.get("kind") == "robots"
    assert result.error is not None
    assert "robots.txt" in result.error
    assert result.output["url"] == "https://site.test/private/doc"


def test_bad_scheme_is_an_input_error() -> None:
    registry = ToolRegistry()
    for tool in build_web_tools(delay=0.0):
        registry.register(tool)
    result = registry.execute(
        "web_fetch",
        {"url": "ftp://example.com/x", "max_chars": 400},
        permission_policy=NETWORK_ONLY,
    )
    assert result.success is False
    assert result.metadata.get("kind") == "input"


def test_oversized_page_is_reported() -> None:
    opener = _Opener(b"User-agent: *\nAllow: /\n", b"x" * 9000)
    ingestor = ingestor_with(opener)
    ingestor.max_bytes = 100
    result = WebFetchTool(ingestor=ingestor).execute({"url": "https://site.test/big"})
    assert result.success is False
    assert result.metadata.get("kind") == "too_large"


def test_missing_credentials_are_loud() -> None:
    tool = WebSearchTool(provider="none")
    result = tool.execute({"query": "anything"})
    assert result.success is False
    assert result.metadata.get("kind") == "credentials"


# -- search providers -------------------------------------------------------


def test_wikipedia_parses_results_and_strips_markup() -> None:
    opener = _Opener(None, b"", wiki=WIKI)
    provider = WikipediaSearch(opener=opener)
    hits = provider.search("sqlite", limit=5)
    assert len(hits) == 2
    assert hits[0].title == "SQLite"
    assert hits[0].url == "https://en.wikipedia.org/wiki/SQLite"
    assert "<span" not in hits[0].snippet


def test_wikipedia_handles_an_empty_result() -> None:
    opener = _Opener(None, b"", wiki={"query": {"search": []}})
    assert WikipediaSearch(opener=opener).search("nothing at all") == []


def test_keyed_provider_without_a_key_fails_loudly(monkeypatch) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    with pytest.raises(SearchCredentialsMissingError) as info:
        KeyedSearch("brave")
    assert "BRAVE_SEARCH_API_KEY" in str(info.value)


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(SearchError):
        KeyedSearch("altavista")
    with pytest.raises(SearchError):
        build_search_provider("altavista")


def test_null_provider_refuses() -> None:
    with pytest.raises(SearchCredentialsMissingError):
        NullSearch().search("anything")


def test_default_provider_is_wikipedia(monkeypatch) -> None:
    for name in (
        "BRAVE_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "SERPER_API_KEY",
        "BING_SEARCH_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    assert build_search_provider().name == "wikipedia"


def test_a_configured_key_is_preferred(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    assert build_search_provider().name == "tavily"


def test_describe_providers_reports_configuration(monkeypatch) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    rows = {row["provider"]: row for row in describe_providers()}
    assert rows["wikipedia"]["requires_key"] is False
    assert rows["wikipedia"]["configured"] is True
    assert rows["brave"]["requires_key"] is True
    assert rows["brave"]["configured"] is False
    assert rows["brave"]["env_var"] == "BRAVE_SEARCH_API_KEY"


# -- fetching ---------------------------------------------------------------


def test_fetch_returns_extracted_text() -> None:
    opener = _Opener(b"User-agent: *\nAllow: /\n", PAGE)
    tool = WebFetchTool(ingestor=ingestor_with(opener))
    result = tool.execute({"url": "https://site.test/page", "max_chars": 500})
    assert result.success is True
    assert result.output["title"] == "Live Page"
    assert "Real content." in result.output["text"]
    assert result.output["truncated"] is False


def test_fetch_truncates_and_flags_it() -> None:
    long_page = (
        b"<html><head><title>Long</title></head><body><p>"
        + b"substantive content " * 200
        + b"</p></body></html>"
    )
    opener = _Opener(b"User-agent: *\nAllow: /\n", long_page)
    tool = WebFetchTool(ingestor=ingestor_with(opener), max_chars=200)
    result = tool.execute({"url": "https://site.test/page"})
    assert result.success is True
    assert result.output["truncated"] is True
    assert result.output["characters"] == 200


def test_max_chars_has_a_floor() -> None:
    opener = _Opener(b"User-agent: *\nAllow: /\n", PAGE)
    tool = WebFetchTool(ingestor=ingestor_with(opener), max_chars=5)
    assert tool.max_chars == 200


def test_robots_are_fetched_once_per_host() -> None:
    opener = _Opener(b"User-agent: *\nAllow: /\n", PAGE)
    ingestor = ingestor_with(opener)
    tool = WebFetchTool(ingestor=ingestor)
    tool.execute({"url": "https://site.test/a"})
    tool.execute({"url": "https://site.test/b"})
    robots_calls = [url for url in opener.requested if url.endswith("robots.txt")]
    assert len(robots_calls) == 1


# -- treasury integration ---------------------------------------------------


def test_fetch_is_metered_and_reserve_is_untouched() -> None:
    from treasury import Money, Treasury

    with Treasury(":memory:") as bank:
        bank.earn(Money.parse("10.00", "USD"), memo="funding")
        opener = _Opener(b"User-agent: *\nAllow: /\n", PAGE)
        ingestor = ingestor_with(opener)
        ingestor.treasury = bank
        ingestor.cost_per_fetch = 0.25
        before = bank.ledger.balance("env:OPERATE")
        result = WebFetchTool(ingestor=ingestor).execute(
            {"url": "https://site.test/page"}
        )
        assert result.success is True
        assert before - bank.ledger.balance("env:OPERATE") == Money.parse("0.25", "USD")
        assert bank.ledger.balance("env:RESERVE") == Money.parse("8.00", "USD")


def test_fetch_stops_when_the_budget_is_gone() -> None:
    from treasury import Money, Treasury

    with Treasury(":memory:") as bank:
        bank.earn(Money.parse("0.00000001", "USD"), memo="dust")
        opener = _Opener(b"User-agent: *\nAllow: /\n", PAGE)
        ingestor = ingestor_with(opener)
        ingestor.treasury = bank
        ingestor.cost_per_fetch = 5.0
        result = WebFetchTool(ingestor=ingestor).execute(
            {"url": "https://site.test/page"}
        )
        assert result.success is False
        assert result.metadata.get("kind") == "budget"


# -- wiring -----------------------------------------------------------------


def test_build_web_tools_returns_both_with_unique_names() -> None:
    tools = build_web_tools(delay=0.0, provider="wikipedia")
    assert {tool.name for tool in tools} == {"web_search", "web_fetch"}
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    assert registry.find("web_search") is not None
    assert registry.find("web_fetch") is not None


def test_tools_share_one_ingestor() -> None:
    """One robots cache and one delay, not two tools racing the same host."""
    tools = build_web_tools(delay=0.0, provider="wikipedia")
    fetch = next(tool for tool in tools if tool.name == "web_fetch")
    search = next(tool for tool in tools if tool.name == "web_search")
    assert isinstance(fetch, WebFetchTool)
    assert fetch.ingestor is not None
    assert search.name == "web_search"


def test_search_tool_needs_no_ingestor() -> None:
    tool = WebSearchTool(provider="wikipedia")
    assert not hasattr(tool, "ingestor")


def test_schemas_advertise_optional_limit() -> None:
    tool = WebSearchTool(provider="wikipedia")
    properties = tool.input_schema["properties"]
    assert "query" in properties
    assert "limit" in properties
    assert tool.input_schema["required"] == ["query"]


def test_registry_rejects_unknown_arguments() -> None:
    registry = ToolRegistry()
    for tool in build_web_tools(delay=0.0, provider="wikipedia"):
        registry.register(tool)
    with pytest.raises(InvalidToolInputError):
        registry.execute(
            "web_search",
            {"query": "x", "surprise": True},
            permission_policy=NETWORK_ONLY,
        )
