"""SQLite persistence for completed transcripts."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


SCHEMA_VERSION = 2


class TranscriptStorageError(RuntimeError):
    """Raised when transcript metadata cannot be persisted safely."""


@dataclass(frozen=True)
class TranscriptRecord:
    """One completed transcript and its persistent artifact locations."""

    telegram_user_id: int
    telegram_chat_id: int
    telegram_message_id: int | None
    created_at: str
    title: str
    text: str
    source_audio_path: str
    artifacts_dir: str


class TranscriptStore:
    """Store completed transcripts in one local SQLite database."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        """Create the versioned schema, or validate an existing database."""
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise TranscriptStorageError(
                        f"Database schema version {version} is newer than supported "
                        f"version {SCHEMA_VERSION}."
                    )
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS transcripts (
                        id                  INTEGER PRIMARY KEY,
                        telegram_user_id    INTEGER NOT NULL,
                        telegram_chat_id    INTEGER NOT NULL,
                        telegram_message_id INTEGER,
                        created_at          TEXT NOT NULL,
                        title               TEXT NOT NULL,
                        text                TEXT NOT NULL,
                        source_audio_path   TEXT NOT NULL UNIQUE,
                        artifacts_dir       TEXT NOT NULL,
                        UNIQUE (telegram_chat_id, telegram_message_id)
                    );

                    CREATE INDEX IF NOT EXISTS ix_transcripts_user_created
                        ON transcripts (telegram_user_id, created_at DESC);

                    CREATE INDEX IF NOT EXISTS ix_transcripts_created
                        ON transcripts (created_at DESC);

                    CREATE TABLE IF NOT EXISTS tags (
                        id          INTEGER PRIMARY KEY,
                        name        TEXT NOT NULL UNIQUE
                                    CHECK (length(trim(name)) > 0),
                        description TEXT NOT NULL
                                    CHECK (length(trim(description)) > 0)
                    );

                    CREATE TABLE IF NOT EXISTS transcript_tags (
                        transcript_id INTEGER NOT NULL
                            REFERENCES transcripts(id) ON DELETE CASCADE,
                        tag_id        INTEGER NOT NULL
                            REFERENCES tags(id) ON DELETE CASCADE,
                        PRIMARY KEY (transcript_id, tag_id)
                    );

                    CREATE INDEX IF NOT EXISTS ix_transcript_tags_tag
                        ON transcript_tags (tag_id, transcript_id);
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except TranscriptStorageError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise TranscriptStorageError(
                f"Could not initialize transcript database: {error}"
            ) from error

    def save(self, record: TranscriptRecord) -> None:
        """Insert or update one completed transcript atomically."""
        self.save_many((record,))

    def save_many(self, records: Iterable[TranscriptRecord]) -> None:
        """Insert or update completed transcripts in one transaction."""
        records = tuple(records)
        if not records:
            return
        for record in records:
            self._validate_record(record)

        try:
            with self._connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO transcripts (
                        telegram_user_id,
                        telegram_chat_id,
                        telegram_message_id,
                        created_at,
                        title,
                        text,
                        source_audio_path,
                        artifacts_dir
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_audio_path) DO UPDATE SET
                        telegram_user_id = excluded.telegram_user_id,
                        telegram_chat_id = excluded.telegram_chat_id,
                        telegram_message_id = excluded.telegram_message_id,
                        created_at = excluded.created_at,
                        title = excluded.title,
                        text = excluded.text,
                        artifacts_dir = excluded.artifacts_dir
                    """,
                    [
                        (
                            record.telegram_user_id,
                            record.telegram_chat_id,
                            record.telegram_message_id,
                            record.created_at,
                            record.title,
                            record.text,
                            record.source_audio_path,
                            record.artifacts_dir,
                        )
                        for record in records
                    ],
                )
        except (OSError, sqlite3.Error) as error:
            raise TranscriptStorageError(
                f"Could not save transcript metadata: {error}"
            ) from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _validate_record(record: TranscriptRecord) -> None:
        if not record.created_at.strip() or not record.title.strip() or not record.text.strip():
            raise TranscriptStorageError(
                "Transcript timestamp, title, and text must not be empty."
            )
        for label, value in (
            ("source audio path", record.source_audio_path),
            ("artifacts directory", record.artifacts_dir),
        ):
            path = PurePosixPath(value)
            if not value or path.is_absolute() or ".." in path.parts:
                raise TranscriptStorageError(
                    f"The {label} must be a safe path relative to the data directory."
                )
