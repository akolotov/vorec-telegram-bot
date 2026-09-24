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
                columns = {
                    row[1]: row[3]
                    for row in connection.execute("PRAGMA table_info(transcripts)")
                }
                indexes = {
                    row[1]
                    for row in connection.execute("PRAGMA index_list(transcripts)")
                }

        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(columns["telegram_message_id"], 0)
        self.assertIn("ix_transcripts_user_created", indexes)
        self.assertIn("ix_transcripts_created", indexes)

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

