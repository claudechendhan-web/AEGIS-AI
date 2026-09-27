from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from core.errors import (
    ConversationNotFoundError,
    DatabaseError,
    InvalidMessageRoleError,
)
from core.models import Conversation, Message, MessageRole


class SQLiteConversationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create_conversation(
        self, metadata: Mapping[str, Any] | None = None
    ) -> Conversation:
        conversation = Conversation(metadata=dict(metadata or {}))
        encoded_metadata = self._encode_metadata(conversation.metadata)
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO conversations
                        (id, created_at, updated_at, metadata)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        conversation.conversation_id,
                        self._encode_datetime(conversation.created_at),
                        self._encode_datetime(conversation.updated_at),
                        encoded_metadata,
                    ),
                )
        except sqlite3.Error as exc:
            raise DatabaseError("unable to create conversation") from exc
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation:
        try:
            row = self._connection.execute(
                """
                SELECT id, created_at, updated_at, metadata
                FROM conversations
                WHERE id = ?
                """,
                (conversation_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to retrieve conversation") from exc
        if row is None:
            raise ConversationNotFoundError(
                f"conversation not found: {conversation_id}"
            )
        messages = self.get_messages(conversation_id)
        return self._conversation_from_row(row, messages)

    def list_conversations(self) -> list[Conversation]:
        try:
            rows = self._connection.execute(
                """
                SELECT id, created_at, updated_at, metadata
                FROM conversations
                ORDER BY created_at ASC, rowid ASC
                """
            ).fetchall()
            return [
                self._conversation_from_row(row, self.get_messages(row["id"]))
                for row in rows
            ]
        except sqlite3.Error as exc:
            raise DatabaseError("unable to list conversations") from exc

    def delete_conversation(self, conversation_id: str) -> None:
        try:
            with self._connection:
                cursor = self._connection.execute(
                    "DELETE FROM conversations WHERE id = ?", (conversation_id,)
                )
        except sqlite3.Error as exc:
            raise DatabaseError("unable to delete conversation") from exc
        if cursor.rowcount == 0:
            raise ConversationNotFoundError(
                f"conversation not found: {conversation_id}"
            )

    def append_message(self, conversation_id: str, message: Message) -> Message:
        self._ensure_conversation_exists(conversation_id)
        try:
            role = MessageRole(message.role).value
        except (TypeError, ValueError) as exc:
            raise InvalidMessageRoleError("message role is invalid") from exc
        encoded_metadata = self._encode_metadata(message.metadata)
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO messages
                        (id, conversation_id, role, content, created_at, metadata)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.message_id,
                        conversation_id,
                        role,
                        message.content,
                        self._encode_datetime(message.created_at),
                        encoded_metadata,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE conversations
                    SET updated_at = ?
                    WHERE id = ?
                    """,
                    (self._encode_datetime(message.created_at), conversation_id),
                )
        except sqlite3.Error as exc:
            raise DatabaseError("unable to append message") from exc
        return message

    def get_messages(self, conversation_id: str) -> list[Message]:
        self._ensure_conversation_exists(conversation_id)
        try:
            rows = self._connection.execute(
                """
                SELECT id, role, content, created_at, metadata
                FROM messages
                WHERE conversation_id = ?
                ORDER BY rowid ASC
                """,
                (conversation_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to retrieve messages") from exc
        return [self._message_from_row(row) for row in rows]

    def save_conversation(self, conversation: Conversation) -> Conversation:
        encoded_metadata = self._encode_metadata(conversation.metadata)
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO conversations
                        (id, created_at, updated_at, metadata)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        metadata = excluded.metadata
                    """,
                    (
                        conversation.conversation_id,
                        self._encode_datetime(conversation.created_at),
                        self._encode_datetime(conversation.updated_at),
                        encoded_metadata,
                    ),
                )
                for message in conversation.messages:
                    role = MessageRole(message.role).value
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO messages
                            (id, conversation_id, role, content, created_at, metadata)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            message.message_id,
                            conversation.conversation_id,
                            role,
                            message.content,
                            self._encode_datetime(message.created_at),
                            self._encode_metadata(message.metadata),
                        ),
                    )
        except sqlite3.Error as exc:
            raise DatabaseError("unable to save conversation") from exc
        return conversation

    def close(self) -> None:
        self._connection.close()

    def _ensure_conversation_exists(self, conversation_id: str) -> None:
        try:
            row = self._connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("unable to verify conversation") from exc
        if row is None:
            raise ConversationNotFoundError(
                f"conversation not found: {conversation_id}"
            )

    def _conversation_from_row(
        self, row: sqlite3.Row, messages: list[Message]
    ) -> Conversation:
        return Conversation(
            conversation_id=row["id"],
            messages=messages,
            created_at=self._decode_datetime(row["created_at"]),
            metadata=self._decode_metadata(row["metadata"]),
            updated_at=self._decode_datetime(row["updated_at"]),
        )

    def _message_from_row(self, row: sqlite3.Row) -> Message:
        try:
            role: MessageRole | str = MessageRole(row["role"])
        except (TypeError, ValueError) as exc:
            raise InvalidMessageRoleError(
                f"stored message has an invalid role: {row['role']}"
            ) from exc
        return Message(
            message_id=row["id"],
            role=role,
            content=row["content"],
            created_at=self._decode_datetime(row["created_at"]),
            metadata=self._decode_metadata(row["metadata"]),
        )

    @staticmethod
    def _encode_datetime(value: datetime) -> str:
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _decode_datetime(value: str) -> datetime:
        try:
            decoded = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise DatabaseError("database contains an invalid timestamp") from exc
        if decoded.tzinfo is None:
            return decoded.replace(tzinfo=UTC)
        return decoded.astimezone(UTC)

    @staticmethod
    def _encode_metadata(metadata: Mapping[str, Any]) -> str:
        try:
            return json.dumps(dict(metadata), sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise DatabaseError("metadata must be JSON serializable") from exc

    @staticmethod
    def _decode_metadata(value: str) -> dict[str, Any]:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DatabaseError("database contains invalid metadata") from exc
        if not isinstance(decoded, dict):
            raise DatabaseError("database metadata must be an object")
        return decoded
