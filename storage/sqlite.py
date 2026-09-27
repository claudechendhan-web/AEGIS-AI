from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from core.errors import DatabaseError
from core.models import Conversation, Message
from storage.base import ConversationRepository
from storage.repositories import SQLiteConversationRepository


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=1,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (
                    role IN ('system', 'user', 'assistant', 'tool')
                ),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
            ON messages (conversation_id)
            """,
            "PRAGMA user_version = 1",
        ),
    ),
)


class SQLiteStorage(ConversationRepository):
    """SQLite-backed conversation storage with automatic migrations."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        timeout: float = 5.0,
    ) -> None:
        if not isinstance(database_path, (str, Path)):
            raise DatabaseError("database_path must be a string or Path")
        memory_database = str(database_path) == ":memory:"
        self.database_path = (
            Path(database_path) if not memory_database else Path(":memory:")
        )
        if not memory_database:
            try:
                self.database_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DatabaseError(
                    f"unable to create database directory: {self.database_path.parent}"
                ) from exc
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                str(self.database_path),
                timeout=timeout,
                check_same_thread=False,
            )
            self._connection: sqlite3.Connection | None = connection
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise DatabaseError("unable to open SQLite database") from exc
        self._repository = SQLiteConversationRepository(connection)
        try:
            self.initialize()
        except DatabaseError:
            connection.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise DatabaseError("SQLite storage is closed")
        return self._connection

    @property
    def schema_version(self) -> int:
        try:
            row = self.connection.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to read schema version") from exc
        return int(row[0]) if row is not None else 0

    def initialize(self) -> None:
        current_version = self.schema_version
        latest_version = MIGRATIONS[-1].version
        if current_version > latest_version:
            raise DatabaseError(
                f"database schema version {current_version} is newer than supported version {latest_version}"
            )
        for migration in MIGRATIONS:
            if current_version >= migration.version:
                continue
            try:
                with self.connection:
                    for statement in migration.statements:
                        self.connection.execute(statement)
            except sqlite3.Error as exc:
                raise DatabaseError(
                    f"unable to apply database migration {migration.version}"
                ) from exc
            current_version = migration.version

    def _require_open(self) -> None:
        if self._connection is None:
            raise DatabaseError("SQLite storage is closed")

    def create_conversation(
        self, metadata: Mapping[str, Any] | None = None
    ) -> Conversation:
        self._require_open()
        return self._repository.create_conversation(metadata)

    def get_conversation(self, conversation_id: str) -> Conversation:
        self._require_open()
        return self._repository.get_conversation(conversation_id)

    def list_conversations(self) -> list[Conversation]:
        self._require_open()
        return self._repository.list_conversations()

    def delete_conversation(self, conversation_id: str) -> None:
        self._require_open()
        self._repository.delete_conversation(conversation_id)

    def append_message(self, conversation_id: str, message: Message) -> Message:
        self._require_open()
        return self._repository.append_message(conversation_id, message)

    def get_messages(self, conversation_id: str) -> list[Message]:
        self._require_open()
        return self._repository.get_messages(conversation_id)

    def save_conversation(self, conversation: Conversation) -> Conversation:
        self._require_open()
        return self._repository.save_conversation(conversation)

    def close(self) -> None:
        if self._connection is not None:
            self._repository.close()
            self._connection = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()
