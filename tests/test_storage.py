from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.errors import (
    ConversationNotFoundError,
    DatabaseError,
    InvalidMessageRoleError,
)
from core.models import Conversation, Message, MessageRole
from storage.sqlite import SQLiteStorage


def test_conversation_creation_retrieval_and_timestamps(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "aegisai.db"
    storage = SQLiteStorage(database_path)
    conversation = storage.create_conversation({"topic": "testing"})

    assert conversation.id
    assert conversation.metadata == {"topic": "testing"}
    assert conversation.created_at.tzinfo == UTC
    assert conversation.updated_at.tzinfo == UTC
    assert storage.schema_version == 1
    assert storage.get_conversation(conversation.id).metadata == {"topic": "testing"}
    assert database_path.exists()
    storage.close()


def test_messages_persist_in_append_order(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "aegisai.db")
    conversation = storage.create_conversation()
    first_time = datetime(2026, 1, 1, tzinfo=UTC)
    messages = [
        Message(
            content="first",
            role=MessageRole.USER,
            created_at=first_time,
        ),
        Message(
            content="second",
            role=MessageRole.ASSISTANT,
            created_at=first_time + timedelta(seconds=1),
        ),
        Message(
            content="third",
            role=MessageRole.TOOL,
            created_at=first_time + timedelta(seconds=2),
        ),
    ]

    for message in messages:
        storage.append_message(conversation.id, message)

    loaded = storage.get_conversation(conversation.id)
    assert [message.content for message in loaded.messages] == [
        "first",
        "second",
        "third",
    ]
    assert [message.role for message in loaded.messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
    ]
    assert loaded.updated_at == messages[-1].created_at
    storage.close()


def test_multiple_conversations_listing_and_deletion(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "aegisai.db")
    first = storage.create_conversation({"name": "first"})
    second = storage.create_conversation({"name": "second"})
    storage.append_message(first.id, Message(content="hello"))

    listed = storage.list_conversations()
    assert [item.id for item in listed] == [first.id, second.id]

    storage.delete_conversation(first.id)
    assert [item.id for item in storage.list_conversations()] == [second.id]
    with pytest.raises(ConversationNotFoundError):
        storage.get_conversation(first.id)
    storage.close()


def test_conversation_persistence_survives_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "aegisai.db"
    storage = SQLiteStorage(database_path)
    conversation = storage.create_conversation({"persistent": True})
    message = Message(content="remember this", role=MessageRole.USER)
    storage.append_message(conversation.id, message)
    storage.close()

    restarted = SQLiteStorage(database_path)
    loaded = restarted.get_conversation(conversation.id)

    assert loaded.metadata == {"persistent": True}
    assert [item.content for item in loaded.messages] == ["remember this"]
    assert loaded.messages[0].id == message.id
    restarted.close()


def test_initialization_is_idempotent_and_preserves_data(tmp_path: Path) -> None:
    database_path = tmp_path / "aegisai.db"
    storage = SQLiteStorage(database_path)
    conversation = storage.create_conversation()

    storage.initialize()
    storage.initialize()

    assert storage.get_conversation(conversation.id).id == conversation.id
    storage.close()


def test_missing_conversation_and_invalid_role_are_explicit(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "aegisai.db")

    with pytest.raises(ConversationNotFoundError):
        storage.get_conversation("missing")
    with pytest.raises(ConversationNotFoundError):
        storage.delete_conversation("missing")
    with pytest.raises(InvalidMessageRoleError):
        Message(content="bad", role="unknown")

    storage.close()


def test_closed_storage_reports_database_error(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "aegisai.db")
    storage.close()

    with pytest.raises(DatabaseError, match="closed"):
        storage.create_conversation()


def test_save_conversation_merges_existing_messages(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "aegisai.db")
    conversation = Conversation(metadata={"source": "test"})
    first = conversation.add_message(Message(content="first"))
    storage.save_conversation(conversation)
    conversation.add_message(Message(content="second"))
    storage.save_conversation(conversation)

    loaded = storage.get_conversation(conversation.id)
    assert [message.content for message in loaded.messages] == ["first", "second"]
    assert loaded.messages[0].id == first.id
    storage.close()
