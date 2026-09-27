"""Errors raised by the retrieval memory.

Defined locally so the memory subsystem can be removed without editing
`core/errors.py` or `memory/__init__.py`.
"""

from core.errors import AegisError


class MemoryError_(AegisError):
    """Base exception for retrieval memory failures."""


class IndexError_(MemoryError_):
    """Raised when the index cannot be opened or written."""


class EmptyQueryError(MemoryError_, ValueError):
    """Raised when a query contains no searchable terms."""


class IngestError(MemoryError_):
    """Raised when a document cannot be fetched or decoded."""


class RobotsDisallowedError(IngestError):
    """Raised when robots.txt forbids fetching a URL.

    Raised rather than logged and skipped, so a caller cannot accidentally
    train a corpus on content it was told not to fetch.
    """


class BudgetExhaustedError(MemoryError_):
    """Raised when an ingest would exceed the caller's spending limit."""


class DocumentTooLargeError(IngestError):
    """Raised when a document exceeds the configured size ceiling."""
