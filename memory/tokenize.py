"""Deterministic tokenization for the lexical index.

No dependencies, no model, no GPU. That is a deliberate constraint: this
machine has a 4GB Fermi-era GPU that modern PyTorch will not target, so a
neural embedding model is not available. BM25 over an inverted index is a
genuinely strong retrieval baseline for this use case and runs on a CPU in
milliseconds, which is the correct trade here rather than a compromise.

The splitter folds a small set of suffixes so that "pricing", "prices" and
"priced" land on one term. It is intentionally crude: a real stemmer would be
better, and a real stemmer would also be a dependency.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "he",
        "her",
        "his",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "you",
        "your",
        "our",
        "not",
        "no",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "may",
        "might",
        "must",
        "shall",
        "about",
        "after",
        "all",
        "also",
        "am",
        "an",
        "as",
        "be",
        "because",
        "been",
        "before",
        "being",
        "between",
        "both",
        "but",
        "by",
        "down",
        "each",
        "few",
        "further",
        "here",
        "him",
        "himself",
        "how",
        "more",
        "most",
        "other",
        "over",
        "same",
        "so",
        "some",
        "such",
        "than",
        "too",
        "very",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
    ]
)

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+#.-]*")
# Identifier separators only. `+`, `#` and `-` stay inside a part so that
# `utf-8`, `c++` and `node.js`-style names survive as single terms.
_SEPARATOR = re.compile(r"[._]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# An acronym run followed by a capitalized word: HTTPResponse -> HTTP, Response.
_ACRONYM = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")

MIN_TOKEN_LENGTH = 2
MAX_TOKEN_LENGTH = 40

_SUFFIXES = (
    "ational",
    "iveness",
    "fulness",
    "ousness",
    "ization",
    "ation",
    "ings",
    "ies",
    "ing",
    "ers",
    "est",
    "ed",
    "es",
    "s",
)


def split_identifier(token: str) -> list[str]:
    """Split `snake_case`, `camelCase`, and `dotted.paths` into parts.

    Code and configuration files are a large part of what is worth indexing,
    and a tokenizer that treats `retry_after_seconds` as one opaque term will
    never match a query for "retry".
    """
    parts: list[str] = []
    for chunk in _SEPARATOR.split(token):
        if not chunk:
            continue
        marked = _ACRONYM.sub("|\x00", _CAMEL.sub("|\x00", chunk))
        parts.extend(piece for piece in marked.split("|\x00") if piece)
    return parts


def stem(token: str) -> str:
    """Strip a common inflectional suffix.

    A bare trailing "s" is left alone on words already ending in "us" or "ss",
    because stripping it turns `status` into `statu` and `focus` into `focu`.
    Since stemming is applied to both the index and the query the damage is
    symmetric and retrieval still matches, but the terms become unreadable in
    the index dump and collide with unrelated words.
    """
    if len(token) <= 3:
        return token
    for suffix in _SUFFIXES:
        if not token.endswith(suffix) or len(token) - len(suffix) < 3:
            continue
        if suffix == "s" and token.endswith(("us", "ss", "is")):
            continue
        if suffix == "es" and not token[:-2].endswith(("s", "x", "z", "ch", "sh")):
            # Strip "es" only after a sibilant (`boxes`, `watches`,
            # `dishes`). Elsewhere the "e" belongs to the stem, so `prices`
            # becomes `price` rather than `pric`.
            continue
        base = token[: -len(suffix)]
        if suffix == "ies":
            return f"{base}y"
        return base
    return token


def tokenize(text: str, *, keep_stopwords: bool = False) -> list[str]:
    """Turn text into normalized index terms.

    Identifier splitting runs on the *original* casing and each piece is
    folded afterwards. Lowercasing first would destroy every camelCase and
    acronym boundary, so `HTTPResponse` would become one opaque
    `httpresponse` term that no query could ever match.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    tokens: list[str] = []
    for raw in _TOKEN.findall(text):
        lowered = raw.lower()
        if MIN_TOKEN_LENGTH <= len(lowered) <= MAX_TOKEN_LENGTH and (
            keep_stopwords or lowered not in STOPWORDS
        ):
            tokens.append(stem(lowered))
        for part in split_identifier(raw):
            folded = part.lower()
            if (
                len(folded) >= MIN_TOKEN_LENGTH
                and folded not in STOPWORDS
                and folded != lowered
            ):
                tokens.append(stem(folded))
    return [token for token in tokens if token]


def term_frequencies(tokens: Iterable[str]) -> dict[str, int]:
    """Count term occurrences."""
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return counts


def unique_terms(text: str) -> set[str]:
    """The distinct searchable terms in a query."""
    return set(tokenize(text))
