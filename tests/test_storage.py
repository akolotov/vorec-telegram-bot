import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vorec.storage import (
    SCHEMA_VERSION,
    TranscriptRecord,
    TranscriptStorageError,
    TranscriptStore,
)


def transcript_record(**overrides) -> TranscriptRecord:
    values = {
        "telegram_user_id": 101,
        "telegram_chat_id": 202,
        "telegram_message_id": 303,
        "created_at": "2026-09-23T10:20:30+00:00",
        "title": "Specific title",
        "text": "Completed transcript",
        "source_audio_path": "voices/2026-09/recording.ogg",
        "artifacts_dir": "transcripts/2026-09/recording",
    }
    values.update(overrides)
    return TranscriptRecord(**values)


class TranscriptStoreTests(unittest.TestCase):
    def test_initializes_versioned_schema_and_indexes_idempotently(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "data" / "vorec.sqlite3"
            store = TranscriptStore(database)

            store.initialize()
            store.initialize()

            with sqlite3.connect(database) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                transcript_columns = {
                    row[1]: row[3]
                    for row in connection.execute("PRAGMA table_info(transcripts)")
                }
                tag_columns = {
                    row[1]: row[3]
                    for row in connection.execute("PRAGMA table_info(tags)")
                }
                transcript_indexes = {
                    row[1]
                    for row in connection.execute("PRAGMA index_list(transcripts)")
                }
                transcript_tag_indexes = {
                    row[1]
                    for row in connection.execute("PRAGMA index_list(transcript_tags)")
                }
                foreign_keys = {
                    (row[2], row[3], row[4], row[6])
                    for row in connection.execute("PRAGMA foreign_key_list(transcript_tags)")
                }

        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(transcript_columns["telegram_message_id"], 0)
        self.assertEqual(tag_columns, {"id": 0, "name": 1, "description": 1})
        self.assertIn("ix_transcripts_user_created", transcript_indexes)
        self.assertIn("ix_transcripts_created", transcript_indexes)
        self.assertIn("ix_transcript_tags_tag", transcript_tag_indexes)
        self.assertEqual(
            foreign_keys,
            {
                ("transcripts", "transcript_id", "id", "CASCADE"),
                ("tags", "tag_id", "id", "CASCADE"),
            },
        )

    def test_migrates_version_one_database_without_losing_transcripts(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "vorec.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE transcripts (
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
                    INSERT INTO transcripts (
                        telegram_user_id, telegram_chat_id, telegram_message_id,
                        created_at, title, text, source_audio_path, artifacts_dir
                    ) VALUES (
                        101, 202, 303, '2026-09-23T10:20:30+00:00',
                        'Existing title', 'Existing transcript',
                        'voices/existing.ogg', 'transcripts/existing'
                    );
                    PRAGMA user_version = 1;
                    """
                )

            TranscriptStore(database).initialize()

            with sqlite3.connect(database) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                transcript = connection.execute(
                    "SELECT title, text FROM transcripts"
                ).fetchone()
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(transcript, ("Existing title", "Existing transcript"))
        self.assertIn("tags", tables)
        self.assertIn("transcript_tags", tables)

    def test_tag_constraints_and_cascading_associations(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "vorec.sqlite3"
            store = TranscriptStore(database)
            store.initialize()
            store.save(transcript_record())

            with store._connect() as connection:
                transcript_id = connection.execute(
                    "SELECT id FROM transcripts"
                ).fetchone()[0]
                tag_id = connection.execute(
                    "INSERT INTO tags (name, description) VALUES (?, ?)",
                    ("Projects", "Notes about work on a specific project."),
                ).lastrowid
                connection.execute(
                    "INSERT INTO transcript_tags (transcript_id, tag_id) VALUES (?, ?)",
                    (transcript_id, tag_id),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO transcript_tags (transcript_id, tag_id) VALUES (?, ?)",
                        (transcript_id, tag_id),
                    )
                connection.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
                associations = connection.execute(
                    "SELECT transcript_id, tag_id FROM transcript_tags"
                ).fetchall()

                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO tags (name, description) VALUES (?, ?)",
                        ("Empty description", "   "),
                    )

        self.assertEqual(associations, [])

    def test_saves_legacy_record_and_updates_it_by_source_path(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "vorec.sqlite3"
            store = TranscriptStore(database)
            store.initialize()
            initial = transcript_record(telegram_message_id=None)
            updated = transcript_record(
                telegram_message_id=None,
                title="Updated title",
                text="Updated transcript",
            )

            store.save(initial)
            store.save(updated)

            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    """
                    SELECT telegram_user_id, telegram_chat_id, telegram_message_id,
                           created_at, title, text, source_audio_path, artifacts_dir
                    FROM transcripts
                    """
                ).fetchall()

        self.assertEqual(
            rows,
            [
                (
                    101,
                    202,
                    None,
                    "2026-09-23T10:20:30+00:00",
                    "Updated title",
                    "Updated transcript",
                    "voices/2026-09/recording.ogg",
                    "transcripts/2026-09/recording",
                )
            ],
        )

    def test_rejects_same_telegram_message_with_different_source(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptStore(Path(directory) / "vorec.sqlite3")
            store.initialize()
            store.save(transcript_record())

            with self.assertRaises(TranscriptStorageError):
                store.save(
                    transcript_record(
                        source_audio_path="voices/2026-09/other.ogg",
                        artifacts_dir="transcripts/2026-09/other",
                    )
                )

    def test_rejects_absolute_or_parent_relative_artifact_paths(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptStore(Path(directory) / "vorec.sqlite3")
            store.initialize()

            for source, artifacts in (
                ("/absolute/audio.ogg", "transcripts/item"),
                ("voices/audio.ogg", "../outside"),
            ):
                with self.subTest(source=source, artifacts=artifacts):
                    with self.assertRaises(TranscriptStorageError):
                        store.save(
                            transcript_record(
                                source_audio_path=source,
                                artifacts_dir=artifacts,
                            )
                        )
