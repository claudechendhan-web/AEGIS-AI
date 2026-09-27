"""Tests for the retrieval memory subsystem.

These check behaviour a naive implementation gets wrong: that identifier
splitting works, that absent context produces no message at all, that the
per-document cap actually caps, and that a robots.txt refusal surfaces instead
of silently indexing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from memory.chunking import chunk_document, chunk_text, document_id_for
from memory.errors import (
    BudgetExhaustedError,
    DocumentTooLargeError,
    EmptyQueryError,
    IngestError,
    RobotsDisallowedError,
)
from memory.ingest import (
    FetchedDocument,
    WebIngestor,
    extract_html_text,
    extract_seed_urls,
)
from memory.retriever import Retriever, estimate_tokens, extract_snippet
from memory.store import MemoryIndex
from memory.tokenize import split_identifier, stem, tokenize


@pytest.fixture()
def index() -> Iterator[MemoryIndex]:
    with MemoryIndex(":memory:") as store:
        yield store


# -- tokenization -----------------------------------------------------------


def test_identifier_splitting() -> None:
    assert split_identifier("retry_after_seconds") == ["retry", "after", "seconds"]
    assert split_identifier("status.code") == ["status", "code"]
    # Case is preserved here and folded later by tokenize().
    assert split_identifier("HTTPResponse") == ["HTTP", "Response"]


def test_code_query_matches_code_document() -> None:
    tokens = tokenize("retry_after_seconds")
    assert "retry" in tokens
    assert "second" in tokens
    # The whole token is indexed too, and each identifier part alongside it.
    folded = tokenize("HTTPResponse.status_code")
    assert "httpresponse.status_code" in folded
    assert {"http", "response", "code"} <= set(folded)


def test_stem_does_not_mangle_us_and_ss_words() -> None:
    assert stem("status") == "status"
    assert stem("focus") == "focus"
    assert stem("prices") == "price"


def test_stem_is_idempotent_for_short_tokens() -> None:
    assert stem("is") == "is"
    assert stem("running") == stem(stem("running")) or stem("running") == "runn"


def test_stopwords_are_dropped() -> None:
    assert "the" not in tokenize("the value of the reserve")


# -- chunking ---------------------------------------------------------------


def test_short_text_is_one_chunk() -> None:
    assert chunk_text("A short document.") == ["A short document."]


def test_empty_text_yields_nothing() -> None:
    assert chunk_text("   \n\n  ") == []


def test_long_text_is_split_with_overlap() -> None:
    text = " ".join(
        f"sentence number {index} carries some information." for index in range(400)
    )
    chunks = chunk_text(text, chunk_tokens=60, overlap_tokens=15)
    assert len(chunks) > 1
    assert all(chunk.strip() for chunk in chunks)


def test_overlap_must_be_smaller_than_chunk() -> None:
    with pytest.raises(ValueError):
        chunk_text("x", chunk_tokens=50, overlap_tokens=50)


def test_document_id_is_stable_and_uri_specific() -> None:
    assert document_id_for("https://x.test/a") == document_id_for("https://x.test/a")
    assert document_id_for("https://x.test/a") != document_id_for("https://x.test/b")


def test_chunk_ids_are_ordinal_and_unique() -> None:
    chunks = chunk_document("https://x.test/a", "para one.\n\npara two.\n\npara three.")
    ids = [chunk.chunk_id for chunk in chunks]
    assert len(ids) == len(set(ids))
    assert ids[0].endswith(":0")


# -- indexing ---------------------------------------------------------------


def test_index_and_search(index: MemoryIndex) -> None:
    index.add_document(
        "https://a.test/treasury",
        "The reserve holds eighty percent of every payment. " * 8,
        title="Treasury",
    )
    hits = index.search("reserve percentage")
    assert hits
    assert hits[0].uri == "https://a.test/treasury"
    assert "reserve" in hits[0].matched_terms


def test_reindexing_a_uri_replaces_it(index: MemoryIndex) -> None:
    uri = "https://a.test/page"
    index.add_document(uri, "alpha beta gamma " * 20, title="First")
    index.add_document(uri, "delta epsilon zeta " * 20, title="Second")
    documents = [doc for doc in index.documents() if doc["uri"] == uri]
    assert len(documents) == 1
    assert documents[0]["title"] == "Second"
    assert index.search("alpha") == []
    assert index.search("delta")


def test_document_frequency_does_not_drift_on_reindex(index: MemoryIndex) -> None:
    uri = "https://a.test/drift"
    for _ in range(5):
        index.add_document(uri, "unique shared tokens here " * 20)
    stats = index.stats()
    index.delete_document(uri)
    assert index.stats().terms <= stats.terms
    assert index.search("shared") == []


def test_empty_document_is_ignored(index: MemoryIndex) -> None:
    assert index.add_document("https://a.test/empty", "   ") == []


def test_search_on_empty_index_returns_nothing(index: MemoryIndex) -> None:
    assert index.search("anything at all") == []


def test_query_without_terms_is_refused(index: MemoryIndex) -> None:
    index.add_document("https://a.test/x", "some real content here " * 20)
    with pytest.raises(EmptyQueryError):
        index.search("the of and")


def test_uri_prefix_filters_results(index: MemoryIndex) -> None:
    index.add_document("https://a.test/one", "shared keyword content " * 20)
    index.add_document("https://b.test/two", "shared keyword content " * 20)
    hits = index.search("shared", uri_prefix="https://a.test/")
    assert hits
    assert all(hit.uri.startswith("https://a.test/") for hit in hits)


def test_delete_document(index: MemoryIndex) -> None:
    index.add_document("https://a.test/gone", "temporary content " * 20)
    assert index.delete_document("https://a.test/gone") is True
    assert index.delete_document("https://a.test/gone") is False
    assert index.search("temporary") == []


# -- retrieval and context --------------------------------------------------


def test_context_carries_citations(index: MemoryIndex) -> None:
    index.add_document(
        "https://a.test/doc", "The reserve ratio is eighty percent. " * 10, title="Doc"
    )
    context = Retriever(index).retrieve("reserve ratio")
    assert context.citations
    assert context.citations[0]["uri"] == "https://a.test/doc"
    assert context.citations[0]["index"] == 1


def test_absent_context_produces_no_message(index: MemoryIndex) -> None:
    index.add_document("https://a.test/doc", "reserve ratio eighty percent " * 10)
    retriever = Retriever(index)
    context = retriever.retrieve("kubernetes ingress controller")
    assert context.is_empty
    assert context.text == ""
    # Critical: absence must produce absence, not a hedging instruction.
    assert retriever.as_system_message("kubernetes ingress controller") is None


def test_present_context_produces_a_message(index: MemoryIndex) -> None:
    index.add_document("https://a.test/doc", "reserve ratio eighty percent " * 10)
    message = Retriever(index).as_system_message("reserve ratio")
    assert message is not None
    assert "reserve" in message


def test_per_document_cap_is_enforced(index: MemoryIndex) -> None:
    body = " ".join(f"reserve fact {index} matters." for index in range(200))
    index.add_document("https://a.test/verbose", body, title="Verbose")
    context = Retriever(index, token_budget=10_000, max_per_document=2).retrieve(
        "reserve fact", limit=10
    )
    assert len(context.hits) <= 2
    assert context.dropped > 0


def test_token_budget_is_respected(index: MemoryIndex) -> None:
    for number in range(5):
        index.add_document(
            f"https://a.test/{number}",
            f"reserve material number {number}. " * 60,
        )
    context = Retriever(index, token_budget=120).retrieve("reserve material", limit=10)
    assert estimate_tokens(context.text) <= 120 * 1.2


def test_snippet_picks_the_relevant_sentence() -> None:
    text = (
        "The sky is blue. " * 20
        + "The reserve ratio is eighty percent. "
        + "Grass grows in spring. " * 20
    )
    snippet = extract_snippet(text, {"reserve"})
    assert "eighty percent" in snippet


def test_empty_query_is_handled_gracefully(index: MemoryIndex) -> None:
    context = Retriever(index).retrieve("the and of")
    assert context.is_empty
    assert context.notes


# -- HTML handling ----------------------------------------------------------


def test_html_text_extraction_drops_script_and_style() -> None:
    html = """
    <html><head><title>My Page</title>
    <style>body { color: red; }</style>
    <script>alert('xss')</script></head>
    <body><h1>Heading</h1><p>Real content here.</p></body></html>
    """
    text, title = extract_html_text(html)
    assert title == "My Page"
    assert "Real content here." in text
    assert "alert" not in text
    assert "color: red" not in text


def test_seed_urls_stay_on_the_same_host() -> None:
    document = FetchedDocument(
        uri="https://a.test/page",
        text="<a href='/one'>1</a><a href='https://b.test/two'>2</a>"
        "<a href='#frag'>f</a><a href='mailto:a@b.test'>m</a>",
    )
    urls = extract_seed_urls(document)
    assert urls == ["https://a.test/one"]


# -- fetching ---------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str = "text/html", status: int = 200):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self, size: int = -1) -> bytes:
        return self._body if size is None or size < 0 else self._body[:size]

    def close(self) -> None:
        pass


class _FakeOpener:
    """Serves robots.txt and pages from a fixed map, recording every request."""

    def __init__(self, pages: dict[str, _FakeResponse], robots: str | None = None):
        self.pages = pages
        self.robots = robots
        self.requested: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requested.append(url)
        if url.endswith("/robots.txt"):
            if self.robots is None:
                return _FakeResponse(b"", "text/plain", 404)
            return _FakeResponse(self.robots.encode(), "text/plain")
        if url in self.pages:
            return self.pages[url]
        return _FakeResponse(b"", "text/plain", 404)


def test_robots_disallow_is_raised_not_swallowed(index: MemoryIndex) -> None:
    opener = _FakeOpener(
        {"https://a.test/page": _FakeResponse(b"<p>secret</p>")},
        robots="User-agent: *\nDisallow: /page",
    )
    ingestor = WebIngestor(index, opener=opener, sleep=lambda _: None)
    with pytest.raises(RobotsDisallowedError):
        ingestor.fetch("https://a.test/page")
    assert index.search("secret") == []


def test_robots_allow_is_indexed(index: MemoryIndex) -> None:
    opener = _FakeOpener(
        {"https://a.test/page": _FakeResponse(b"<title>T</title><p>allowed body</p>")},
        robots="User-agent: *\nAllow: /",
    )
    ingestor = WebIngestor(index, opener=opener, sleep=lambda _: None)
    document = ingestor.fetch("https://a.test/page")
    assert "allowed body" in document.text
    assert document.title == "T"


def test_unsupported_content_type_is_refused(index: MemoryIndex) -> None:
    opener = _FakeOpener(
        {"https://a.test/f": _FakeResponse(b"\x00\x01", "application/octet-stream")}
    )
    ingestor = WebIngestor(index, opener=opener, sleep=lambda _: None)
    with pytest.raises(IngestError):
        ingestor.fetch("https://a.test/f")


def test_oversized_document_is_refused(index: MemoryIndex) -> None:
    opener = _FakeOpener(
        {"https://a.test/big": _FakeResponse(b"x" * 5000, "text/plain")}
    )
    ingestor = WebIngestor(index, opener=opener, sleep=lambda _: None, max_bytes=1000)
    with pytest.raises(DocumentTooLargeError):
        ingestor.fetch("https://a.test/big")


def test_ingest_report_separates_refusals_from_failures(index: MemoryIndex) -> None:
    opener = _FakeOpener(
        {
            "https://a.test/good": _FakeResponse(b"<p>good body</p>"),
            "https://a.test/bad": _FakeResponse(b"", "text/plain", 500),
        },
        robots="User-agent: *\nDisallow: /bad",
    )
    ingestor = WebIngestor(index, opener=opener, sleep=lambda _: None)
    report = ingestor.ingest_urls(
        ["https://a.test/good", "https://a.test/bad", "https://a.test/missing"]
    )
    assert report.indexed == ("https://a.test/good",)
    assert report.refused == ("https://a.test/bad",)
    assert len(report.failed) == 1
    assert index.search("good body")


def test_ingest_refuses_when_the_budget_is_gone(index: MemoryIndex) -> None:
    from treasury import Money, Treasury

    with Treasury(":memory:") as bank:
        bank.earn(Money.parse("0.00000001", "USD"), memo="almost nothing")
        opener = _FakeOpener({"https://a.test/p": _FakeResponse(b"<p>x</p>")})
        ingestor = WebIngestor(
            index, treasury=bank, opener=opener, sleep=lambda _: None
        )
        ingestor.cost_per_fetch = 1.0
        with pytest.raises(BudgetExhaustedError):
            ingestor.fetch("https://a.test/p")


def test_ingest_charges_the_treasury(index: MemoryIndex) -> None:
    from treasury import Money, Treasury

    with Treasury(":memory:") as bank:
        bank.earn(Money.parse("10.00", "USD"), memo="funding")
        opener = _FakeOpener({"https://a.test/p": _FakeResponse(b"<p>body</p>")})
        ingestor = WebIngestor(
            index, treasury=bank, opener=opener, sleep=lambda _: None
        )
        ingestor.cost_per_fetch = 0.01
        before = bank.ledger.balance("env:OPERATE")
        ingestor.fetch("https://a.test/p")
        after = bank.ledger.balance("env:OPERATE")
        assert before - after == Money.parse("0.01", "USD")
        # The reserve is untouched by research spending.
        assert bank.ledger.balance("env:RESERVE") == Money.parse("8.00", "USD")


def test_local_paths_are_free(index: MemoryIndex, tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("The reserve ratio is eighty percent.\n", encoding="utf-8")
    from treasury import Money, Treasury

    with Treasury(":memory:") as bank:
        bank.earn(Money.parse("1.00", "USD"), memo="funding")
        before = bank.ledger.balance("env:OPERATE")
        report = WebIngestor(index, treasury=bank).ingest_paths([target])
        assert report.indexed
        assert bank.ledger.balance("env:OPERATE") == before
    assert index.search("reserve ratio")
