"""Splitting documents into index-sized chunks.

Chunk boundaries decide retrieval quality more than the ranking function does.
Split on paragraph structure, keep a small overlap so a fact that straddles a
boundary is still retrievable from either side, and never emit a chunk so
large that it cannot fit in a prompt.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

DEFAULT_CHUNK_TOKENS = 220
DEFAULT_OVERLAP_TOKENS = 40

_PARAGRAPH = re.compile(r"\n\s*\n+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE.sub(" ", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def _approximate_tokens(text: str) -> int:
    """Cheap token estimate: no tokenizer call, no model load.

    Deliberately an estimate. Overestimating chunk size is harmless here; the
    real budget is enforced by the provider's own token count at call time.
    """
    return max(1, len(text) // 4)


def _split_long_unit(unit: str, limit: int) -> Iterator[str]:
    """Break an oversized paragraph or sentence on sentence then word bounds."""
    if _approximate_tokens(unit) <= limit:
        yield unit
        return
    buffer: list[str] = []
    for sentence in _SENTENCE.split(unit):
        if _approximate_tokens(" ".join(buffer + [sentence])) > limit and buffer:
            yield " ".join(buffer)
            buffer = []
        if _approximate_tokens(sentence) > limit:
            if buffer:
                yield " ".join(buffer)
                buffer = []
            words: list[str] = []
            for word in sentence.split():
                if _approximate_tokens(" ".join(words + [word])) > limit and words:
                    yield " ".join(words)
                    words = []
                words.append(word)
            if words:
                yield " ".join(words)
            continue
        buffer.append(sentence)
    if buffer:
        yield " ".join(buffer)


def chunk_text(
    text: str,
    *,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[str]:
    """Split `text` into overlapping chunks of roughly `chunk_tokens`."""
    if chunk_tokens <= 0:
        raise ValueError("chunk_tokens must be positive")
    if overlap_tokens < 0 or overlap_tokens >= chunk_tokens:
        raise ValueError("overlap_tokens must be in [0, chunk_tokens)")
    normalized = _normalize(text)
    if not normalized:
        return []
    units: list[str] = []
    for paragraph in _PARAGRAPH.split(normalized):
        paragraph = paragraph.strip()
        if paragraph:
            units.extend(_split_long_unit(paragraph, chunk_tokens))
    if not units:
        return []

    chunks: list[str] = []
    current: list[str] = []
    for unit in units:
        candidate = " ".join(current + [unit])
        if current and _approximate_tokens(candidate) > chunk_tokens:
            chunks.append(" ".join(current).strip())
            # Carry the tail of the previous chunk forward as overlap.
            tail: list[str] = []
            budget = 0
            for piece in reversed(current):
                size = _approximate_tokens(piece)
                if budget + size > overlap_tokens:
                    break
                tail.insert(0, piece)
                budget += size
            current = tail
        current.append(unit)
    if current:
        final = " ".join(current).strip()
        if final and (not chunks or final != chunks[-1]):
            chunks.append(final)
    return [chunk for chunk in chunks if chunk]


@dataclass(frozen=True, slots=True)
class Chunk:
    """One indexable unit of a document."""

    chunk_id: str
    document_id: str
    ordinal: int
    text: str
    token_estimate: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "ordinal": self.ordinal,
            "text": self.text,
            "token_estimate": self.token_estimate,
        }


def document_id_for(uri: str) -> str:
    """Stable id for a source URI, so re-ingesting replaces rather than duplicates."""
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("uri must be a non-empty string")
    return hashlib.sha256(uri.strip().encode("utf-8")).hexdigest()[:32]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_document(
    uri: str,
    text: str,
    *,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Chunk a document and stamp stable ids on the pieces."""
    document_id = document_id_for(uri)
    pieces = chunk_text(text, chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens)
    return [
        Chunk(
            chunk_id=f"{document_id}:{ordinal}",
            document_id=document_id,
            ordinal=ordinal,
            text=piece,
            token_estimate=_approximate_tokens(piece),
        )
        for ordinal, piece in enumerate(pieces)
    ]
