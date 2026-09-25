"""SQLite persistence for completed transcripts."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


SCHEMA_VERSION = 3


class TranscriptStorageError(RuntimeError):
    """Raised when transcript metadata cannot be persisted safely."""


class TagValidationError(TranscriptStorageError):
    """A tag name or description is invalid."""


class TagConflictError(TranscriptStorageError):
    """A user already has a tag with this name."""


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


@dataclass(frozen=True)
class TagRecord:
    id: int
    name: str


@dataclass(frozen=True)
class TagDefinition(TagRecord):
    description: str


@dataclass(frozen=True)
class TranscriptSummary:
    id: int
    created_at: str
    title: str
    tags: tuple[TagRecord, ...]


@dataclass(frozen=True)
class TranscriptDetail(TranscriptSummary):
    text: str


def normalize_tag_name(name: str) -> str:
    """Return a personal tag's canonical name without its display prefix."""
    normalized = name.strip().lstrip("#").strip().casefold()
    if not normalized:
        raise ValueError("A tag name must not be empty.")
    return normalized


class TranscriptStore:
    """Store completed transcripts in one local SQLite database."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        """Create the versioned schema, or validate an existing database."""
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise TranscriptStorageError(
                        f"Database schema version {version} is newer than supported "
                        f"version {SCHEMA_VERSION}."
                    )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS transcripts (
                        id INTEGER PRIMARY KEY,
                        telegram_user_id INTEGER NOT NULL,
                        telegram_chat_id INTEGER NOT NULL,
                        telegram_message_id INTEGER,
                        created_at TEXT NOT NULL,
                        title TEXT NOT NULL,
                        text TEXT NOT NULL,
                        source_audio_path TEXT NOT NULL UNIQUE,
                        artifacts_dir TEXT NOT NULL,
                        UNIQUE (telegram_chat_id, telegram_message_id)
                    )"""
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS ix_transcripts_user_created "
                    "ON transcripts (telegram_user_id, created_at DESC)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS ix_transcripts_created "
                    "ON transcripts (created_at DESC)"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_transcripts_id_user "
                    "ON transcripts (id, telegram_user_id)"
                )

                if version < SCHEMA_VERSION:
                    tables = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    for table in ("tags", "transcript_tags"):
                        if table in tables and connection.execute(
                            f"SELECT 1 FROM {table} LIMIT 1"
                        ).fetchone() is not None:
                            raise TranscriptStorageError(
                                "Existing tags cannot be migrated automatically."
                            )
                    connection.execute("DROP TABLE IF EXISTS transcript_tags")
                    connection.execute("DROP TABLE IF EXISTS tags")

                connection.execute(
                    """CREATE TABLE IF NOT EXISTS tags (
                        id INTEGER PRIMARY KEY,
                        telegram_user_id INTEGER NOT NULL,
                        name TEXT NOT NULL CHECK (length(trim(name)) > 0),
                        description TEXT NOT NULL CHECK (length(trim(description)) > 0),
                        UNIQUE (telegram_user_id, name),
                        UNIQUE (id, telegram_user_id)
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS transcript_tags (
                        transcript_id INTEGER NOT NULL,
                        tag_id INTEGER NOT NULL,
                        telegram_user_id INTEGER NOT NULL,
                        PRIMARY KEY (transcript_id, tag_id),
                        FOREIGN KEY (transcript_id, telegram_user_id)
                            REFERENCES transcripts(id, telegram_user_id) ON DELETE CASCADE,
                        FOREIGN KEY (tag_id, telegram_user_id)
                            REFERENCES tags(id, telegram_user_id) ON DELETE CASCADE
                    )"""
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS ix_transcript_tags_tag "
                    "ON transcript_tags (tag_id, transcript_id)"
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

    def save_with_tags(self, record: TranscriptRecord, tag_names: tuple[str, ...]) -> None:
        """Save a transcript and replace its personal tag links in one transaction."""
        self._validate_record(record)
        if len(tag_names) != len(set(tag_names)):
            raise TranscriptStorageError("Transcript tags must be unique.")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                tag_ids: list[int] = []
                for name in tag_names:
                    row = connection.execute(
                        "SELECT id FROM tags WHERE telegram_user_id = ? AND name = ?",
                        (record.telegram_user_id, name),
                    ).fetchone()
                    if row is not None:
                        tag_ids.append(row[0])
                self._save_records(connection, (record,))
                transcript_id = connection.execute(
                    "SELECT id FROM transcripts WHERE source_audio_path = ?",
                    (record.source_audio_path,),
                ).fetchone()[0]
                connection.execute(
                    "DELETE FROM transcript_tags WHERE transcript_id = ?", (transcript_id,)
                )
                connection.executemany(
                    "INSERT INTO transcript_tags (transcript_id, tag_id, telegram_user_id) "
                    "VALUES (?, ?, ?)",
                    [(transcript_id, tag_id, record.telegram_user_id) for tag_id in tag_ids],
                )
        except TranscriptStorageError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise TranscriptStorageError(
                f"Could not save transcript metadata: {error}"
            ) from error

    def save_many(self, records: Iterable[TranscriptRecord]) -> None:
        """Insert or update completed transcripts in one transaction."""
        records = tuple(records)
        if not records:
            return
        for record in records:
            self._validate_record(record)

        try:
            with self._connect() as connection:
                self._save_records(connection, records)
        except (OSError, sqlite3.Error) as error:
            raise TranscriptStorageError(
                f"Could not save transcript metadata: {error}"
            ) from error

    @staticmethod
    def _save_records(
        connection: sqlite3.Connection, records: tuple[TranscriptRecord, ...]
    ) -> None:
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

    def create_tag(
        self, telegram_user_id: int, name: str, description: str
    ) -> TagDefinition:
        """Persist one canonical personal tag."""
        canonical_name, description = self._tag_values(name, description)
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO tags (telegram_user_id, name, description) VALUES (?, ?, ?)",
                    (telegram_user_id, canonical_name, description),
                )
                return TagDefinition(cursor.lastrowid, canonical_name, description)
        except sqlite3.IntegrityError as error:
            raise TagConflictError("A tag with this name already exists.") from error
        except (OSError, sqlite3.Error) as error:
            raise TranscriptStorageError("Could not create personal tag.") from error

    @staticmethod
    def _tag_values(name: str, description: str) -> tuple[str, str]:
        try:
            canonical_name = normalize_tag_name(name)
        except (ValueError, AttributeError) as error:
            raise TagValidationError("A tag name must not be empty.") from error
        description = description.strip()
        if not description:
            raise TagValidationError("A tag description must not be empty.")
        return canonical_name, description

    def update_tag(
        self, telegram_user_id: int, tag_id: int, name: str, description: str
    ) -> TagDefinition | None:
        """Change a tag only when it belongs to this user."""
        canonical_name, description = self._tag_values(name, description)
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE tags SET name = ?, description = ? "
                    "WHERE id = ? AND telegram_user_id = ?",
                    (canonical_name, description, tag_id, telegram_user_id),
                )
                if not cursor.rowcount:
                    return None
                return TagDefinition(tag_id, canonical_name, description)
        except sqlite3.IntegrityError as error:
            raise TagConflictError("A tag with this name already exists.") from error
        except (OSError, sqlite3.Error) as error:
            raise TranscriptStorageError("Could not update personal tag.") from error

    def delete_tag(self, telegram_user_id: int, tag_id: int) -> bool:
        """Delete an owned tag and let foreign keys remove its transcript links."""
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM tags WHERE id = ? AND telegram_user_id = ?",
                    (tag_id, telegram_user_id),
                )
                return bool(cursor.rowcount)
        except (OSError, sqlite3.Error) as error:
            raise TranscriptStorageError("Could not delete personal tag.") from error

    def list_tags_for_user(self, telegram_user_id: int) -> tuple[TagDefinition, ...]:
        """Read names and descriptions available for one user's classification."""
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT id, name, description FROM tags "
                    "WHERE telegram_user_id = ? ORDER BY name, id",
                    (telegram_user_id,),
                ).fetchall()
        except (OSError, sqlite3.Error) as error:
            raise TranscriptStorageError("Could not read personal tags.") from error
        return tuple(TagDefinition(*row) for row in rows)

    def list_for_user(self, telegram_user_id: int) -> list[TranscriptSummary]:
        """Read a user's transcript metadata and personal tags in one query."""
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """SELECT t.id, t.created_at, t.title, tag.id, tag.name
                    FROM transcripts AS t
                    LEFT JOIN transcript_tags AS tt
                        ON tt.transcript_id = t.id
                    LEFT JOIN tags AS tag ON tag.id = tt.tag_id
                    WHERE t.telegram_user_id = ?
                    ORDER BY t.created_at DESC, t.id DESC, tag.name, tag.id""",
                    (telegram_user_id,),
                ).fetchall()
        except (OSError, sqlite3.Error) as error:
            raise TranscriptStorageError("Could not read transcripts.") from error

        summaries: list[TranscriptSummary] = []
        for transcript_id, created_at, title, tag_id, tag_name in rows:
            if not summaries or summaries[-1].id != transcript_id:
                summaries.append(
                    TranscriptSummary(transcript_id, created_at, title, ())
                )
            if tag_id is not None:
                current = summaries[-1]
                summaries[-1] = TranscriptSummary(
                    current.id,
                    current.created_at,
                    current.title,
                    (*current.tags, TagRecord(tag_id, tag_name)),
                )
        return summaries

    def get_for_user(
        self, transcript_id: int, telegram_user_id: int
    ) -> TranscriptDetail | None:
        """Read one transcript only when it belongs to the requested user."""
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """SELECT t.id, t.created_at, t.title, t.text, tag.id, tag.name
                    FROM transcripts AS t
                    LEFT JOIN transcript_tags AS tt ON tt.transcript_id = t.id
                    LEFT JOIN tags AS tag ON tag.id = tt.tag_id
                    WHERE t.id = ? AND t.telegram_user_id = ?
                    ORDER BY tag.name, tag.id""",
                    (transcript_id, telegram_user_id),
                ).fetchall()
        except (OSError, sqlite3.Error) as error:
            raise TranscriptStorageError("Could not read transcript.") from error
        if not rows:
            return None
        _, created_at, title, content, _, _ = rows[0]
        tags = tuple(
            TagRecord(tag_id, tag_name)
            for _, _, _, _, tag_id, tag_name in rows
            if tag_id is not None
        )
        return TranscriptDetail(transcript_id, created_at, title, tags, content)

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
