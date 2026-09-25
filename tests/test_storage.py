import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vorec.storage import (
    SCHEMA_VERSION,
    TagConflictError,
    TagValidationError,
    TranscriptRecord,
    TranscriptStorageError,
    TranscriptStore,
    normalize_tag_name,
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
    def test_tag_management_preserves_transcripts_and_enforces_ownership(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptStore(Path(directory) / "vorec.sqlite3")
            store.initialize()
            store.save(transcript_record())
            first = store.create_tag(101, " #Work ", " Work notes ")
            other = store.create_tag(202, "work", "Other user's tag")
            store.save_with_tags(transcript_record(), (first.id,))

            self.assertEqual(first.name, "work")
            self.assertEqual(first.description, "Work notes")
            self.assertEqual(store.list_tags_for_user(101), (first,))
            self.assertIsNone(store.update_tag(101, other.id, "changed", "Changed"))
            self.assertFalse(store.delete_tag(101, other.id))
            self.assertEqual(store.list_tags_for_user(202), (other,))
            with self.assertRaises(TagConflictError):
                store.create_tag(101, "WORK", "Duplicate")
            with self.assertRaises(TagValidationError):
                store.create_tag(101, " ", "Description")
            with self.assertRaises(TagValidationError):
                store.update_tag(101, first.id, "work", " ")
            duplicate = store.create_tag(101, "travel", "Travel notes")
            with self.assertRaises(TagConflictError):
                store.update_tag(101, first.id, "TRAVEL", "Other description")

            updated = store.update_tag(101, first.id, " #Projects ", " Projects and plans ")
            self.assertEqual((updated.name, updated.description), ("projects", "Projects and plans"))
            self.assertEqual(store.list_for_user(101)[0].tags[0].name, "projects")
            self.assertEqual(store.list_tags_for_user(101)[1], duplicate)
            self.assertTrue(store.delete_tag(101, first.id))
            self.assertEqual(store.list_for_user(101)[0].tags, ())
            self.assertIsNotNone(store.get_for_user(store.list_for_user(101)[0].id, 101))
            self.assertEqual(store.list_tags_for_user(202), (other,))

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
        self.assertEqual(
            tag_columns,
            {"id": 0, "telegram_user_id": 1, "name": 1, "description": 1},
        )
        self.assertIn("ix_transcripts_user_created", transcript_indexes)
        self.assertIn("ix_transcripts_created", transcript_indexes)
        self.assertIn("ux_transcripts_id_user", transcript_indexes)
        self.assertIn("ix_transcript_tags_tag", transcript_tag_indexes)
        self.assertEqual(
            foreign_keys,
            {
                ("transcripts", "transcript_id", "id", "CASCADE"),
                ("transcripts", "telegram_user_id", "telegram_user_id", "CASCADE"),
                ("tags", "tag_id", "id", "CASCADE"),
                ("tags", "telegram_user_id", "telegram_user_id", "CASCADE"),
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
                    "INSERT INTO tags (telegram_user_id, name, description) VALUES (?, ?, ?)",
                    (101, "projects", "Notes about work on a specific project."),
                ).lastrowid
                connection.execute(
                    "INSERT INTO transcript_tags (transcript_id, tag_id, telegram_user_id) VALUES (?, ?, ?)",
                    (transcript_id, tag_id, 101),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO transcript_tags (transcript_id, tag_id, telegram_user_id) VALUES (?, ?, ?)",
                        (transcript_id, tag_id, 101),
                    )
                connection.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
                associations = connection.execute(
                    "SELECT transcript_id, tag_id FROM transcript_tags"
                ).fetchall()

                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO tags (telegram_user_id, name, description) VALUES (?, ?, ?)",
                        (101, "empty-description", "   "),
                    )

        self.assertEqual(associations, [])

    def test_personal_tags_and_composite_foreign_keys(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptStore(Path(directory) / "vorec.sqlite3")
            store.initialize()
            store.save(transcript_record())
            store.save(
                transcript_record(
                    telegram_user_id=202,
                    telegram_chat_id=404,
                    telegram_message_id=505,
                    source_audio_path="voices/other.ogg",
                    artifacts_dir="transcripts/other",
                )
            )
            with store._connect() as connection:
                first_id, second_id = [
                    row[0] for row in connection.execute("SELECT id FROM transcripts ORDER BY id")
                ]
                first_tag = store.create_tag(101, " #WORK ", "Work notes").id
                second_tag = store.create_tag(202, "work", "Work notes").id
                self.assertEqual(
                    connection.execute("SELECT name FROM tags WHERE id = ?", (first_tag,)).fetchone()[0],
                    "work",
                )
                with self.assertRaises(TranscriptStorageError):
                    store.create_tag(101, "Work", "Duplicate")
                connection.execute(
                    "INSERT INTO transcript_tags VALUES (?, ?, 101)", (first_id, first_tag)
                )
                connection.execute(
                    "INSERT INTO transcript_tags VALUES (?, ?, 202)", (second_id, second_tag)
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO transcript_tags VALUES (?, ?, 101)",
                        (first_id, second_tag),
                    )
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                connection.execute("DELETE FROM transcripts WHERE id = ?", (first_id,))
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM transcript_tags").fetchone()[0],
                    1,
                )

    def test_save_with_tags_replaces_links_and_keeps_title_atomic(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptStore(Path(directory) / "vorec.sqlite3")
            store.initialize()
            work = store.create_tag(101, "work", "Work notes")
            travel = store.create_tag(101, "travel", "Trips")
            store.create_tag(202, "work", "Another user's work notes")
            self.assertEqual(
                [(tag.name, tag.description) for tag in store.list_tags_for_user(101)],
                [("travel", "Trips"), ("work", "Work notes")],
            )
            store.save_with_tags(
                transcript_record(
                    telegram_user_id=202,
                    telegram_chat_id=404,
                    telegram_message_id=505,
                    source_audio_path="voices/other.ogg",
                    artifacts_dir="transcripts/other",
                ),
                (travel.id,),
            )
            other = store.list_for_user(202)
            self.assertEqual(len(other), 1)
            self.assertEqual(other[0].tags, ())

            store.save_with_tags(transcript_record(), (work.id, travel.id))
            detail = store.get_for_user(store.list_for_user(101)[0].id, 101)
            self.assertEqual([tag.name for tag in detail.tags], ["travel", "work"])

            updated = transcript_record(title="Updated title")
            with self.assertRaisesRegex(TranscriptStorageError, "unique"):
                store.save_with_tags(updated, (work.id, work.id))
            detail = store.get_for_user(detail.id, 101)
            self.assertEqual(detail.title, "Specific title")
            self.assertEqual([tag.name for tag in detail.tags], ["travel", "work"])

            store.save_with_tags(updated, ())
            detail = store.get_for_user(detail.id, 101)
            self.assertEqual(detail.title, "Updated title")
            self.assertEqual(detail.tags, ())

    def test_save_with_tags_skips_tag_deleted_during_transcription(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptStore(Path(directory) / "vorec.sqlite3")
            store.initialize()
            store.create_tag(101, "work", "Work notes")
            store.create_tag(101, "travel", "Trips")
            available_tags = store.list_tags_for_user(101)

            deleted_id = next(tag.id for tag in available_tags if tag.name == "travel")
            store.delete_tag(101, deleted_id)
            store.create_tag(101, "travel", "New trips")

            store.save_with_tags(
                transcript_record(), tuple(tag.id for tag in available_tags)
            )
            records = store.list_for_user(101)
            self.assertEqual(len(records), 1)
            self.assertEqual([tag.name for tag in records[0].tags], ["work"])

    def test_save_with_tags_keeps_tag_renamed_during_transcription(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptStore(Path(directory) / "vorec.sqlite3")
            store.initialize()
            selected = store.create_tag(101, "work", "Work notes")

            store.update_tag(101, selected.id, "projects", "Project notes")
            store.save_with_tags(transcript_record(), (selected.id,))

            self.assertEqual(
                [tag.name for tag in store.list_for_user(101)[0].tags], ["projects"]
            )

    def test_migrates_empty_version_two_tags_and_refuses_nonempty_tags(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "vorec.sqlite3"
            store = TranscriptStore(database)
            store.initialize()
            store.save(transcript_record())
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """DROP TABLE transcript_tags;
                    DROP TABLE tags;
                    CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                        description TEXT NOT NULL);
                    CREATE TABLE transcript_tags (transcript_id INTEGER, tag_id INTEGER);
                    PRAGMA user_version = 2;"""
                )
                connection.execute(
                    "INSERT INTO tags (name, description) VALUES ('old', 'Old tag')"
                )
            with self.assertRaisesRegex(TranscriptStorageError, "cannot be migrated"):
                store.initialize()
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0], 1)
                connection.execute("DELETE FROM tags")
            store.initialize()
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(
                    connection.execute("SELECT title FROM transcripts").fetchone()[0],
                    "Specific title",
                )
                self.assertEqual(
                    connection.execute("PRAGMA table_info(tags)").fetchall()[1][1],
                    "telegram_user_id",
                )

    def test_migrates_version_three_tags_without_reusing_ids(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptStore(Path(directory) / "vorec.sqlite3")
            store.initialize()
            original = store.create_tag(101, "work", "Work notes")
            store.save_with_tags(transcript_record(), (original.id,))
            with store._connect() as connection:
                connection.execute("DROP TRIGGER reserve_tag_id")
                connection.execute("DROP TABLE tag_id_allocations")
                connection.execute("PRAGMA user_version = 3")

            store.initialize()
            self.assertEqual(store.list_for_user(101)[0].tags[0].id, original.id)
            store.delete_tag(101, original.id)
            replacement = store.create_tag(101, "work", "New work notes")
            self.assertGreater(replacement.id, original.id)
            self.assertEqual(store.list_for_user(101)[0].tags, ())
            with store._connect() as connection:
                external_id = connection.execute(
                    "INSERT INTO tags (telegram_user_id, name, description) "
                    "VALUES (101, 'travel', 'Trip notes')"
                ).lastrowid
            self.assertGreater(store.create_tag(101, "home", "Home notes").id, external_id)

    def test_reads_only_own_transcripts_and_tags(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptStore(Path(directory) / "vorec.sqlite3")
            store.initialize()
            store.save(transcript_record())
            store.save(
                transcript_record(
                    telegram_user_id=202,
                    telegram_chat_id=404,
                    telegram_message_id=505,
                    title="Other title",
                    source_audio_path="voices/other.ogg",
                    artifacts_dir="transcripts/other",
                )
            )
            with store._connect() as connection:
                own_id = connection.execute(
                    "SELECT id FROM transcripts WHERE telegram_user_id = 101"
                ).fetchone()[0]
                other_id = connection.execute(
                    "SELECT id FROM transcripts WHERE telegram_user_id = 202"
                ).fetchone()[0]
                tag_id = connection.execute(
                    "INSERT INTO tags (telegram_user_id, name, description) VALUES (101, 'work', 'Work')"
                ).lastrowid
                connection.execute(
                    "INSERT INTO transcript_tags VALUES (?, ?, 101)", (own_id, tag_id)
                )
            summaries = store.list_for_user(101)
            detail = store.get_for_user(own_id, 101)
            self.assertEqual([item.id for item in summaries], [own_id])
            self.assertEqual([tag.name for tag in summaries[0].tags], ["work"])
            self.assertEqual(detail.text, "Completed transcript")
            self.assertIsNone(store.get_for_user(other_id, 101))
            self.assertEqual(normalize_tag_name(" #WOrK "), "work")

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
