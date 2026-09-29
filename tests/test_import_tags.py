import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vorec.import_tags import TagImportError, import_default_tags, load_default_tags
from vorec.storage import TranscriptRecord, TranscriptStore


class DefaultTagImportTests(unittest.TestCase):
    def test_imports_all_tags_into_empty_database_and_refuses_repeat(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "vorec.sqlite3"
            store = TranscriptStore(database)
            store.initialize()

            count = import_default_tags(database, user_id=101)
            actual = store.list_tags_for_user(101)
            expected = load_default_tags()

            self.assertEqual(count, len(expected))
            self.assertEqual(
                {tag.name: tag.description for tag in actual}, dict(expected)
            )
            with self.assertRaisesRegex(TagImportError, "already contains tags"):
                import_default_tags(database, user_id=101)
            self.assertEqual(store.list_tags_for_user(101), actual)

    def test_imports_into_older_empty_tag_schema(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "vorec.sqlite3"
            store = TranscriptStore(database)
            store.initialize()
            with sqlite3.connect(database) as connection:
                connection.execute("DROP TRIGGER reserve_tag_id")
                connection.execute("DROP TABLE tag_id_allocations")
                connection.execute("PRAGMA user_version = 3")

            self.assertEqual(import_default_tags(database, user_id=101), 17)
            self.assertEqual(len(store.list_tags_for_user(101)), 17)

    def test_refuses_wrong_owner_without_importing_tags(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "vorec.sqlite3"
            store = TranscriptStore(database)
            store.initialize()
            store.save(
                TranscriptRecord(
                    telegram_user_id=101,
                    telegram_chat_id=202,
                    telegram_message_id=303,
                    created_at="2026-09-29T12:00:00+00:00",
                    title="Example",
                    text="Example transcript",
                    source_audio_path="voices/example.ogg",
                    artifacts_dir="transcripts/example",
                )
            )

            with self.assertRaisesRegex(TagImportError, "does not own"):
                import_default_tags(database, user_id=404)
            self.assertEqual(store.list_tags_for_user(101), ())
            self.assertEqual(store.list_tags_for_user(404), ())

    def test_refuses_missing_database(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "missing.sqlite3"
            with self.assertRaisesRegex(TagImportError, "does not exist"):
                import_default_tags(database, user_id=101)
            self.assertFalse(database.exists())

    def test_refuses_uninitialized_sqlite_file(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "other.sqlite3"
            sqlite3.connect(database).close()

            with self.assertRaisesRegex(TagImportError, "not initialized for Vorec"):
                import_default_tags(database, user_id=101)
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
                    ).fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
