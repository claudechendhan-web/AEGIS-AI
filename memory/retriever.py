"""Turning search results into prompt context.

The job here is not just "concatenate the top hits". Retrieved text that
floods the prompt degrades answer quality, and text without provenance
produces confident-sounding claims nobody can check. So this module does three
things:

* **Trims to a budget.** Chunks are admitted in rank order until a token
  estimate is reached, and a chunk that does not fit is skipped rather than
  truncated into a misleading fragment.
* **Deduplicates by document.** Ten chunks from one page is one source, not
  ten. A per-document cap keeps a single verbose page from crowding out
  everything else.
* **Keeps citations.** Every admitted chunk carries its URI, so the model can
  be asked to attribute and the reader can check.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from memory.store import Hit, MemoryIndex

DEFAULT_TOKEN_BUDGET = 1200
DEFAULT_MAX_PER_DOCUMENT = 2

NO_CONTEXT = ""
NO_CONTEXT_NOTE = "(no relevant information was found in local memory)"

_SENTENCE = re.compile(r"(?<=[.!?])\s+")

DEFAULT_TEMPLATE = (
    "Reference material retrieved from the local index. Use it when it is "
    "relevant, ignore it when it is not, and say so when it does not answer "
    "the question. Cite sources as [1], [2] and so on.\n\n{context}"
)

FOLLOW_UP_TEMPLATE = (
    "Reference material retrieved from the local index:\n\n{context}\n\n"
    "The user asked a follow-up question. The material above is background, "
    "not necessarily a direct answer."
)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def extract_snippet(
    text: str, query_terms: Iterable[str], *, sentences: int = 2
) -> str:
    """Return the sentences in `text` that best cover `query_terms`.

    A 220-token chunk often buries its one relevant sentence in the middle.
    Selecting the densest window keeps the evidence and drops the padding.
    """
    wanted = {term for term in query_terms if term}
    if not wanted:
        return text
    parts = [part for part in _SENTENCE.split(text) if part.strip()]
    if len(parts) <= sentences:
        return text
    best_index = 0
    best_score = -1.0
    for start in range(max(1, len(parts) - sentences + 1)):
        window = parts[start : start + sentences]
        lowered = " ".join(window).lower()
        score = sum(lowered.count(term) for term in wanted)
        if score > best_score:
            best_score = score
            best_index = start
    return " ".join(parts[best_index : best_index + sentences]).strip()


@dataclass(frozen=True, slots=True)
class Context:
    """Assembled prompt context plus the provenance needed to audit it."""

    text: str
    citations: tuple[dict[str, Any], ...] = ()
    hits: tuple[Hit, ...] = ()
    dropped: int = 0
    notes: tuple[str, ...] = field(default=())

    @property
    def is_empty(self) -> bool:
        return not self.hits

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "citations": [dict(citation) for citation in self.citations],
            "hits": [hit.to_dict() for hit in self.hits],
            "dropped": self.dropped,
            "notes": list(self.notes),
        }


class Retriever:
    """Retrieval with a budget, a per-document cap, and citations."""

    def __init__(
        self,
        index: MemoryIndex,
        *,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        max_per_document: int = DEFAULT_MAX_PER_DOCUMENT,
    ) -> None:
        self.index = index
        self.token_budget = token_budget
        self.max_per_document = max_per_document

    def retrieve(
        self,
        query: str,
        *,
        limit: int = 8,
        min_score: float = 0.0,
        uri_prefix: str | None = None,
        snippets: bool = True,
    ) -> Context:
        if limit <= 0 or self.token_budget <= 0:
            return Context(text=NO_CONTEXT)

        from memory.errors import EmptyQueryError
        from memory.tokenize import tokenize

        try:
            hits = self.index.search(
                query, limit=limit, min_score=min_score, uri_prefix=uri_prefix
            )
            query_terms = set(tokenize(query))
        except EmptyQueryError:
            return Context(
                text=NO_CONTEXT,
                notes=("query had no searchable terms",),
            )

        if not hits:
            return Context(
                text=NO_CONTEXT,
                notes=(NO_CONTEXT_NOTE,),
            )

        admitted: list[tuple[int, Hit, str]] = []
        per_document: dict[str, int] = {}
        used = 0
        dropped = 0
        for hit in hits:
            seen = per_document.get(hit.document_id, 0)
            if seen >= self.max_per_document:
                dropped += 1
                continue
            body = extract_snippet(hit.text, query_terms) if snippets else hit.text
            number = len(admitted) + 1
            header = f"[{number}] {hit.citation}\n"
            # The header counts against the budget. Omitting it lets the
            # assembled prompt exceed the limit by one line per citation,
            # which is exactly the kind of drift that silently crowds out the
            # model's real instructions.
            cost = estimate_tokens(body) + estimate_tokens(header)
            if used + cost > self.token_budget:
                dropped += 1
                continue
            per_document[hit.document_id] = seen + 1
            used += cost
            admitted.append(
                (number, hit if body == hit.text else _replace_text(hit, body), body)
            )

        if not admitted:
            return Context(
                text=NO_CONTEXT,
                hits=tuple(hits),
                dropped=dropped,
                notes=("nothing fit within the context budget",),
            )

        lines = []
        citations = []
        for number, hit, body in admitted:
            lines.append(f"[{number}] {hit.citation}\n{body}")
            citations.append(
                {
                    "index": number,
                    "uri": hit.uri,
                    "title": hit.title,
                    "score": round(hit.score, 6),
                    "matched_terms": list(hit.matched_terms),
                }
            )
        return Context(
            text="\n\n".join(lines),
            citations=tuple(citations),
            hits=tuple(hit for _, hit, _ in admitted),
            dropped=dropped,
        )

    def as_system_message(
        self,
        query: str,
        *,
        template: str = DEFAULT_TEMPLATE,
        **kwargs: Any,
    ) -> str | None:
        """Render retrieved context as a system message, or None if empty.

        Returning None matters: injecting "no relevant information found"
        into every prompt teaches the model to hedge even when it knows the
        answer, so absence of context should produce absence of a message.
        """
        context = self.retrieve(query, **kwargs)
        if context.is_empty:
            return None
        return template.format(context=context.text)


def _replace_text(hit: Hit, text: str) -> Hit:
    return Hit(
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        uri=hit.uri,
        title=hit.title,
        text=text,
        score=hit.score,
        ordinal=hit.ordinal,
        matched_terms=hit.matched_terms,
        metadata=dict(hit.metadata),
    )


def build_context_block(
    context: Context, template: str = DEFAULT_TEMPLATE
) -> Mapping[str, str] | None:
    """Render a `Context` as a single-message mapping for a chat provider."""
    if context.is_empty:
        return None
    return {"role": "system", "content": template.format(context=context.text)}
