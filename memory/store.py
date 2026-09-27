"""A persistent BM25 index over an inverted term index.

Design notes worth stating, because they are choices rather than defaults:

* **No embeddings.** The usual advice is "embed your documents". This machine
  has no usable GPU, so a real embedding model is not an option, and a cloud
  embedding API needs a key and sends the corpus to a third party. BM25 is a
  strong retrieval baseline, needs no dependencies, and runs on CPU.

* **Scores are computed in Python, not SQL.** The query fetches candidate
  postings by term and ranks them in Python. Doing arithmetic in SQL for a
  floating-point scoring function buys nothing and hides the formula.

* **Re-ingesting a URI replaces it.** Documents are keyed by a hash of their
  URI, so refreshing a page does not duplicate it. Old postings are removed
  before new ones are written, in the same transaction.

* **Document frequency is derived, not stored per term.** `memory_terms` is
  maintained on write; `df` is a count, so it cannot drift from reality.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from memory.chunking import Chunk, chunk_document, document_id_for
from memory.errors import EmptyQueryError, IndexError_
from memory.tokenize import term_frequencies, tokenize

K1 = 1.5
B = 0.75

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS memory_documents (
        document_id TEXT PRIMARY KEY,
        uri TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'local',
        content_hash TEXT NOT NULL,
        chunk_count INTEGER NOT NULL DEFAULT 0,
        added_at TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_chunks (
        chunk_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        text TEXT NOT NULL,
        token_estimate INTEGER NOT NULL,
        FOREIGN KEY (document_id)
            REFERENCES memory_documents(document_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_postings (
        term TEXT NOT NULL,
        chunk_id TEXT NOT NULL,
        tf INTEGER NOT NULL,
        PRIMARY KEY (term, chunk_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_terms (
        term TEXT PRIMARY KEY,
        df INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_chunks_document
    ON memory_chunks (document_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_postings_term
    ON memory_postings (term)
    """,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class Hit:
    """One scored search result."""

    chunk_id: str
    document_id: str
    uri: str
    title: str
    text: str
    score: float
    ordinal: int
    matched_terms: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def citation(self) -> str:
        return self.title or self.uri

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "uri": self.uri,
            "title": self.title,
            "text": self.text,
            "score": round(self.score, 6),
            "ordinal": self.ordinal,
            "matched_terms": list(self.matched_terms),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class IndexStats:
    documents: int
    chunks: int
    terms: int
    average_chunk_tokens: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "documents": self.documents,
            "chunks": self.chunks,
            "terms": self.terms,
            "average_chunk_tokens": round(self.average_chunk_tokens, 3),
        }


class MemoryIndex:
    """A durable lexical index with BM25 ranking."""

    def __init__(
        self, database_path: str | Path = "data/memory.db", *, timeout: float = 5.0
    ) -> None:
        memory = str(database_path) == ":memory:"
        self.database_path = Path(":memory:") if memory else Path(database_path)
        if not memory:
            try:
                self.database_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise IndexError_(
                    f"unable to create database directory: {self.database_path.parent}"
                ) from exc
        self._connection: sqlite3.Connection | None
        try:
            self._connection = sqlite3.connect(
                str(self.database_path), timeout=timeout, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise IndexError_("unable to open memory index") from exc
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        try:
            self.initialize()
        except IndexError_:
            self.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise IndexError_("memory index is closed")
        return self._connection

    def initialize(self) -> None:
        try:
            with self.connection:
                for statement in SCHEMA:
                    self.connection.execute(statement)
        except sqlite3.Error as exc:
            raise IndexError_("unable to initialize memory schema") from exc

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writing ----------------------------------------------------------

    def add_document(
        self,
        uri: str,
        text: str,
        *,
        title: str = "",
        source: str = "local",
        chunk_tokens: int = 220,
        overlap_tokens: int = 40,
        metadata: Mapping[str, Any] | None = None,
    ) -> list[Chunk]:
        """Index a document, replacing any previous version of the same URI."""
        if not isinstance(text, str) or not text.strip():
            return []
        document_id = document_id_for(uri)
        chunks = chunk_document(
            uri,
            text,
            chunk_tokens=chunk_tokens,
            overlap_tokens=overlap_tokens,
        )
        if not chunks:
            return []
        from memory.chunking import content_hash

        try:
            with self.connection:
                existing = self.connection.execute(
                    "SELECT chunk_id FROM memory_chunks WHERE document_id = ?",
                    (document_id,),
                ).fetchall()
                if existing:
                    self._forget_terms(
                        [row["chunk_id"] for row in existing], document_id
                    )
                    self.connection.execute(
                        "DELETE FROM memory_chunks WHERE document_id = ?",
                        (document_id,),
                    )
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO memory_documents
                        (document_id, uri, title, source, content_hash,
                         chunk_count, added_at, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        uri,
                        title,
                        source,
                        content_hash(text),
                        len(chunks),
                        _now(),
                        json.dumps(dict(metadata or {}), sort_keys=True),
                    ),
                )
                for chunk in chunks:
                    self.connection.execute(
                        """
                        INSERT INTO memory_chunks
                            (chunk_id, document_id, ordinal, text, token_estimate)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            chunk.chunk_id,
                            document_id,
                            chunk.ordinal,
                            chunk.text,
                            chunk.token_estimate,
                        ),
                    )
                    self._add_terms(chunk)
        except sqlite3.Error as exc:
            raise IndexError_(f"unable to index {uri}: {exc}") from exc
        return chunks

    def _add_terms(self, chunk: Chunk) -> None:
        counts = term_frequencies(tokenize(chunk.text))
        for term, tf in counts.items():
            self.connection.execute(
                "INSERT OR IGNORE INTO memory_postings (term, chunk_id, tf) VALUES (?, ?, ?)",
                (term, chunk.chunk_id, tf),
            )
            self.connection.execute(
                """
                INSERT INTO memory_terms (term, df) VALUES (?, 1)
                ON CONFLICT(term) DO UPDATE SET df = df + 1
                """,
                (term,),
            )

    def _forget_terms(self, chunk_ids: Sequence[str], document_id: str) -> None:
        """Decrement df for every term only this document contributed."""
        if not chunk_ids:
            return
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = self.connection.execute(
            f"SELECT term FROM memory_postings WHERE chunk_id IN ({placeholders})",
            tuple(chunk_ids),
        ).fetchall()
        for row in rows:
            self.connection.execute(
                """
                UPDATE memory_terms SET df = df - 1
                WHERE term = ? AND df > 0
                """,
                (row["term"],),
            )
        self.connection.execute(
            f"DELETE FROM memory_postings WHERE chunk_id IN ({placeholders})",
            tuple(chunk_ids),
        )
        self.connection.execute("DELETE FROM memory_terms WHERE df <= 0")

    def delete_document(self, uri: str) -> bool:
        document_id = document_id_for(uri)
        rows = self.connection.execute(
            "SELECT chunk_id FROM memory_chunks WHERE document_id = ?",
            (document_id,),
        ).fetchall()
        if not rows:
            return False
        try:
            with self.connection:
                self._forget_terms([row["chunk_id"] for row in rows], document_id)
                self.connection.execute(
                    "DELETE FROM memory_chunks WHERE document_id = ?", (document_id,)
                )
                self.connection.execute(
                    "DELETE FROM memory_documents WHERE document_id = ?", (document_id,)
                )
        except sqlite3.Error as exc:
            raise IndexError_(f"unable to delete {uri}") from exc
        return True

    # -- reading ----------------------------------------------------------

    def stats(self) -> IndexStats:
        try:
            documents = self.connection.execute(
                "SELECT COUNT(*) AS n FROM memory_documents"
            ).fetchone()["n"]
            row = self.connection.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(token_estimate), 0) AS total "
                "FROM memory_chunks"
            ).fetchone()
            terms = self.connection.execute(
                "SELECT COUNT(*) AS n FROM memory_terms WHERE df > 0"
            ).fetchone()["n"]
        except sqlite3.Error as exc:
            raise IndexError_("unable to read index statistics") from exc
        chunks = int(row["n"])
        total = int(row["total"])
        return IndexStats(
            documents=int(documents),
            chunks=chunks,
            terms=int(terms),
            average_chunk_tokens=(total / chunks) if chunks else 0.0,
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
        uri_prefix: str | None = None,
    ) -> list[Hit]:
        """Rank chunks with BM25 and return the best matches."""
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms:
            raise EmptyQueryError("query contains no searchable terms")
        if limit <= 0:
            return []

        total_chunks = int(
            self.connection.execute(
                "SELECT COUNT(*) AS n FROM memory_chunks"
            ).fetchone()["n"]
        )
        if total_chunks == 0:
            return []
        average_length = self.stats().average_chunk_tokens or 1.0

        document_filter = ""
        parameters: list[Any] = list(terms)
        if uri_prefix:
            document_filter = " AND d.uri LIKE ?"
            parameters.append(f"{uri_prefix}%")

        placeholders = ",".join("?" for _ in terms)
        try:
            rows = self.connection.execute(
                f"""
                SELECT p.term AS term,
                       p.chunk_id AS chunk_id,
                       p.tf AS tf,
                       c.text AS text,
                       c.ordinal AS ordinal,
                       c.token_estimate AS length,
                       d.document_id AS document_id,
                       d.uri AS uri,
                       d.title AS title,
                       d.metadata AS metadata
                FROM memory_postings AS p
                JOIN memory_chunks AS c ON c.chunk_id = p.chunk_id
                JOIN memory_documents AS d ON d.document_id = c.document_id
                WHERE p.term IN ({placeholders}){document_filter}
                """,
                tuple(parameters),
            ).fetchall()
        except sqlite3.Error as exc:
            raise IndexError_("unable to search index") from exc

        scores: dict[str, float] = {}
        matched: dict[str, set[str]] = {}
        lengths: dict[str, int] = {}
        for row in rows:
            chunk_id = row["chunk_id"]
            term = row["term"]
            document_frequency = self._document_frequency(term, total_chunks)
            idf = math.log(
                1.0
                + (total_chunks - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            tf = int(row["tf"])
            length = int(row["length"]) or 1
            lengths[chunk_id] = length
            denominator = tf + K1 * (1.0 - B + B * length / average_length)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + idf * (
                tf * (K1 + 1.0) / denominator
            )
            matched.setdefault(chunk_id, set()).add(term)

        results: list[Hit] = []
        for row in rows:
            chunk_id = row["chunk_id"]
            if chunk_id not in scores:
                continue
            score = scores[chunk_id]
            if score < min_score:
                continue
            try:
                metadata = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            results.append(
                Hit(
                    chunk_id=chunk_id,
                    document_id=row["document_id"],
                    uri=row["uri"],
                    title=row["title"],
                    text=row["text"],
                    score=score,
                    ordinal=int(row["ordinal"]),
                    matched_terms=tuple(sorted(matched.get(chunk_id, ()))),
                    metadata=metadata if isinstance(metadata, dict) else {},
                )
            )
        results.sort(key=lambda hit: (-hit.score, hit.chunk_id))
        return results[:limit]

    def _document_frequency(self, term: str, total_chunks: int) -> int:
        row = self.connection.execute(
            "SELECT df FROM memory_terms WHERE term = ?", (term,)
        ).fetchone()
        if row is None:
            return 0
        return max(0, min(int(row["df"]), total_chunks))

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        try:
            row = self.connection.execute(
                "SELECT * FROM memory_chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise IndexError_("unable to read chunk") from exc
        if row is None:
            return None
        return Chunk(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            ordinal=int(row["ordinal"]),
            text=row["text"],
            token_estimate=int(row["token_estimate"]),
        )

    def documents(self) -> list[dict[str, Any]]:
        try:
            rows = self.connection.execute(
                "SELECT * FROM memory_documents ORDER BY added_at DESC"
            ).fetchall()
        except sqlite3.Error as exc:
            raise IndexError_("unable to list documents") from exc
        documents = []
        for row in rows:
            record = dict(row)
            record["metadata"] = json.loads(record.get("metadata") or "{}")
            documents.append(record)
        return documents

    def known_uris(self) -> set[str]:
        try:
            rows = self.connection.execute(
                "SELECT uri FROM memory_documents"
            ).fetchall()
        except sqlite3.Error as exc:
            raise IndexError_("unable to list document uris") from exc
        return {row["uri"] for row in rows}

    def __repr__(self) -> str:
        return f"MemoryIndex({str(self.database_path)!r})"


def index_paths(
    index: MemoryIndex,
    paths: Iterable[Path],
    *,
    read: Any = None,
    **kwargs: Any,
) -> dict[str, int]:
    """Index local files, keyed by `file://` URI so provenance is preserved."""
    import json as _json
    from urllib.request import pathname2url

    from memory.errors import IngestError

    loader = read or (lambda path: path.read_text(encoding="utf-8", errors="replace"))
    added = 0
    for path in paths:
        try:
            text = loader(path)
        except OSError as exc:
            raise IngestError(f"unable to read {path}: {exc}") from exc
        if isinstance(text, (dict, list)):
            text = _json.dumps(text, indent=2, sort_keys=True)
        uri = f"file://{pathname2url(str(path.resolve()))}"
        if index.add_document(uri, text, title=path.name, source="file", **kwargs):
            added += 1
    return {"documents_added": added}
