import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vorec.import_transcripts import (
    ArchiveImportError,
    collect_records,
    main,
    regenerate_titles_and_tags,
)
from vorec.storage import TranscriptRecord, TranscriptStorageError, TranscriptStore


class TranscriptImportTests(unittest.TestCase):
    @staticmethod
    def add_recording(
        data_directory: Path,
        recording_id: str,
        *,
        merged: str = "Merged transcript",
        title: str | None = None,
        extension: str = ".ogg",
    ) -> None:
        month = recording_id[:7]
        artifacts = data_directory / "transcripts" / month / recording_id
        artifacts.mkdir(parents=True)
        (artifacts / "merged.txt").write_text(merged + "\n", encoding="utf-8")
        if title is not None:
            (artifacts / "title.txt").write_text(title + "\n", encoding="utf-8")
        source_directory = data_directory / "voices" / month
        source_directory.mkdir(parents=True, exist_ok=True)
        (source_directory / f"{recording_id}{extension}").touch()

    def test_collects_current_and_legacy_records_for_explicit_owner(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory)
            current_id = "2026-09-23_10-20-30_222_333"
            legacy_id = "2026-08-22_09-10-11"
            self.add_recording(data, current_id, title="Stored title")
            self.add_recording(data, legacy_id, merged="L" * 60, extension=".m4a")
            orphan = data / "voices" / "2026-09" / "orphan.ogg"
            orphan.touch()

            plan = collect_records(data, user_id=111, chat_id=222)

        self.assertEqual(len(plan.records), 2)
        self.assertEqual(plan.orphan_audio_count, 1)
        current = next(
            record for record in plan.records if record.telegram_message_id is not None
        )
        legacy = next(
            record for record in plan.records if record.telegram_message_id is None
        )
        self.assertEqual(current.telegram_user_id, 111)
        self.assertEqual(current.telegram_chat_id, 222)
        self.assertEqual(current.telegram_message_id, 333)
        self.assertEqual(current.title, "Stored title")
        self.assertEqual(current.created_at, "2026-09-23T10:20:30+00:00")
        self.assertEqual(
            current.source_audio_path,
            f"voices/2026-09/{current_id}.ogg",
        )
        self.assertEqual(legacy.title, "L" * 50)
        self.assertEqual(
            legacy.artifacts_dir,
            f"transcripts/2026-08/{legacy_id}",
        )

    def test_rejects_current_record_from_another_chat(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory)
            self.add_recording(data, "2026-09-23_10-20-30_999_333")

            with self.assertRaisesRegex(ArchiveImportError, "not the requested chat"):
                collect_records(data, user_id=111, chat_id=222)

    def test_empty_stored_title_uses_transcript_prefix(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory)
            recording_id = "2026-09-23_10-20-30_222_333"
            self.add_recording(data, recording_id, merged="M" * 60, title="")

            plan = collect_records(data, user_id=111, chat_id=222)

        self.assertEqual(plan.records[0].title, "M" * 50)

    def test_ignores_incomplete_transcript_directory(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory)
            self.add_recording(data, "2026-09-23_10-20-30_222_333")
            incomplete_id = "2026-09-23_10-21-30_222_334"
            incomplete = data / "transcripts" / "2026-09" / incomplete_id
            incomplete.mkdir(parents=True)
            (incomplete / "primary.txt").write_text("partial\n", encoding="utf-8")
            (data / "voices" / "2026-09" / f"{incomplete_id}.ogg").touch()

            plan = collect_records(data, user_id=111, chat_id=222)

        self.assertEqual(len(plan.records), 1)
        self.assertEqual(plan.orphan_audio_count, 1)

    def test_rejects_invalid_calendar_timestamp(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory)
            self.add_recording(data, "2026-02-31_10-20-30_222_333")

            with self.assertRaisesRegex(ArchiveImportError, "invalid timestamp"):
                collect_records(data, user_id=111, chat_id=222)

    def test_rejects_archive_before_writing_when_any_record_is_invalid(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory)
            self.add_recording(data, "2026-09-23_10-20-30_222_333")
            invalid = data / "transcripts" / "2026-09" / "unknown-format"
            invalid.mkdir(parents=True)
            (invalid / "merged.txt").write_text("text\n", encoding="utf-8")

            with self.assertRaises(SystemExit):
                main(
                    [
                        "--data-directory",
                        str(data),
                        "--user-id",
                        "111",
                        "--chat-id",
                        "222",
                    ]
                )

            self.assertFalse((data / "vorec.sqlite3").exists())

    def test_dry_run_validates_without_creating_database(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory)
            self.add_recording(data, "2026-09-23_10-20-30_222_333")
            output = io.StringIO()

            with (
                redirect_stdout(output),
                patch("vorec.import_transcripts.load_dotenv") as load_env,
                patch("vorec.import_transcripts.OpenAI") as client_class,
            ):
                result = main(
                    [
                        "--data-directory",
                        str(data),
                        "--user-id",
                        "111",
                        "--chat-id",
                        "222",
                        "--dry-run",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertIn("Validated 1 transcript(s)", output.getvalue())
            self.assertFalse((data / "vorec.sqlite3").exists())
            load_env.assert_not_called()
            client_class.assert_not_called()

    def test_import_then_regenerate_after_tags_are_added(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory)
            self.add_recording(
                data, "2026-09-23_10-20-30_222_333", merged="Archive text",
                title="Archive title",
            )
            store = TranscriptStore(data / "vorec.sqlite3")
            with (
                patch("vorec.import_transcripts.load_dotenv") as load_env,
                patch("vorec.import_transcripts.OpenAI") as client_class,
                patch("vorec.import_transcripts.generate_transcript_title") as generation,
                redirect_stdout(io.StringIO()) as import_output,
            ):
                result = main([
                    "--data-directory", str(data), "--user-id", "111", "--chat-id", "222",
                ])
            self.assertEqual(result, 0)
            self.assertIn("Imported 1 transcript(s)", import_output.getvalue())
            self.assertEqual(store.list_for_user(111)[0].title, "Archive title")
            load_env.assert_not_called()
            client_class.assert_not_called()
            generation.assert_not_called()

            archive_tag = store.create_tag(111, "archive", "Archive notes")
            old_tag = store.create_tag(222, "old", "Old selection")
            new_tag = store.create_tag(222, "new", "New selection")
            existing = TranscriptRecord(
                telegram_user_id=222, telegram_chat_id=444, telegram_message_id=555,
                created_at="2026-09-22T10:20:30+00:00", title="Existing title",
                text="Existing text", source_audio_path="voices/existing.ogg",
                artifacts_dir="transcripts/existing",
            )
            store.save_with_tags(existing, (old_tag.id,))

            def generate(text, client, model, tags):
                self.assertIs(client, mock_client)
                self.assertEqual(model, "test-model")
                if text == "Existing text":
                    self.assertEqual({tag.id for tag in tags}, {old_tag.id, new_tag.id})
                    return {}, "Generated existing", ("new",)
                self.assertEqual(text, "Archive text")
                self.assertEqual(tags, (archive_tag,))
                return {}, "Generated archive", ("archive",)

            mock_client = Mock()
            with (
                patch.dict(os.environ, {
                    "INFERENCE_API_URL": "http://localhost:8111/v1",
                    "INFERENCE_API_KEY": "test-key",
                    "TITLE_MODEL": "test-model",
                }),
                patch("vorec.import_transcripts.load_dotenv"),
                patch("vorec.import_transcripts.OpenAI", return_value=mock_client),
                patch("vorec.import_transcripts.collect_records") as collect,
                patch("vorec.import_transcripts.generate_transcript_title", side_effect=generate) as generation,
                redirect_stdout(io.StringIO()) as output,
            ):
                result = main([
                    "--data-directory", str(data), "--regenerate-titles-and-tags",
                ])

            self.assertEqual(result, 0)
            collect.assert_not_called()
            self.assertEqual(generation.call_count, 2)
            self.assertIn("2/2 stored transcript(s)", output.getvalue())
            own = store.list_for_user(111)[0]
            other = store.list_for_user(222)[0]
            self.assertEqual(
                (own.title, [tag.id for tag in own.tags]),
                ("Generated archive", [archive_tag.id]),
            )
            self.assertEqual(
                (other.title, [tag.id for tag in other.tags]),
                ("Generated existing", [new_tag.id]),
            )

    def test_regeneration_requires_existing_database_and_separate_arguments(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory)
            with self.assertRaises(SystemExit):
                main(["--data-directory", str(data), "--regenerate-titles-and-tags"])
            self.assertFalse((data / "vorec.sqlite3").exists())
            with self.assertRaises(SystemExit):
                main(["--data-directory", str(data), "--regenerate-titles-and-tags", "--dry-run"])
            with self.assertRaises(SystemExit):
                main(["--data-directory", str(data)])

    def test_main_reports_generation_failure_with_zero_exit_status(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory)
            self.add_recording(data, "2026-09-23_10-20-30_222_333", title="Archive title")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([
                    "--data-directory", str(data), "--user-id", "111", "--chat-id", "222",
                ]), 0)
            store = TranscriptStore(data / "vorec.sqlite3")
            tag = store.create_tag(111, "notes", "Personal notes")
            store.save_with_tags(
                collect_records(data, user_id=111, chat_id=222).records[0], (tag.id,)
            )
            output = io.StringIO()

            with (
                patch.dict(os.environ, {
                    "INFERENCE_API_URL": "http://localhost:8111/v1",
                    "INFERENCE_API_KEY": "test-key",
                }),
                patch("vorec.import_transcripts.load_dotenv"),
                patch("vorec.import_transcripts.OpenAI"),
                patch("vorec.import_transcripts.generate_transcript_title", side_effect=RuntimeError("provider error")),
                redirect_stdout(output),
            ):
                result = main([
                    "--data-directory", str(data), "--regenerate-titles-and-tags",
                ])

            self.assertEqual(result, 0)
            self.assertIn("1 generation failure(s)", output.getvalue())
            summary = store.list_for_user(111)[0]
            self.assertEqual(summary.title, "Archive title")
            self.assertEqual(summary.tags, ())

    def test_generation_failure_keeps_title_clears_tags_and_continues(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptStore(Path(directory) / "vorec.sqlite3")
            store.initialize()
            tag = store.create_tag(111, "notes", "Personal notes")
            first = TranscriptRecord(
                telegram_user_id=111, telegram_chat_id=222, telegram_message_id=1,
                created_at="2026-09-22T10:20:30+00:00", title="Keep this title",
                text="First text", source_audio_path="voices/first.ogg",
                artifacts_dir="transcripts/first",
            )
            second = TranscriptRecord(
                telegram_user_id=333, telegram_chat_id=444, telegram_message_id=2,
                created_at="2026-09-23T10:20:30+00:00", title="Other title",
                text="Second text", source_audio_path="voices/second.ogg",
                artifacts_dir="transcripts/second",
            )
            store.save_with_tags(first, (tag.id,))
            store.save(second)
            output = io.StringIO()

            with (
                patch("vorec.import_transcripts.generate_transcript_title", side_effect=[
                    RuntimeError("provider error"), ({}, "New title", ()),
                ]) as generation,
                redirect_stdout(output),
            ):
                count, failures = regenerate_titles_and_tags(store, Mock(), "test-model")

            self.assertEqual((count, failures), (2, 1))
            self.assertEqual(generation.call_count, 2)
            self.assertEqual(generation.call_args_list[1].args[3], ())
            self.assertIn("keeping its title and clearing tags", output.getvalue())
            self.assertEqual(store.list_for_user(111)[0].title, "Keep this title")
            self.assertEqual(store.list_for_user(111)[0].tags, ())
            self.assertEqual(store.list_for_user(333)[0].title, "New title")

    def test_regeneration_selects_only_requested_database_ids(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory)
            store = TranscriptStore(data / "vorec.sqlite3")
            store.initialize()
            tag = store.create_tag(111, "notes", "Personal notes")
            for number in range(1, 4):
                store.save_with_tags(TranscriptRecord(
                    telegram_user_id=111, telegram_chat_id=222,
                    telegram_message_id=number,
                    created_at=f"2026-09-2{number}T10:20:30+00:00",
                    title=f"Original {number}", text=f"Text {number}",
                    source_audio_path=f"voices/{number}.ogg",
                    artifacts_dir=f"transcripts/{number}",
                ), (tag.id,))
            ids = [record_id for record_id, _ in store.list_all_records_with_ids()]

            with (
                patch.dict(os.environ, {
                    "INFERENCE_API_URL": "http://localhost:8111/v1",
                    "INFERENCE_API_KEY": "test-key",
                }),
                patch("vorec.import_transcripts.load_dotenv"),
                patch("vorec.import_transcripts.OpenAI"),
                patch(
                    "vorec.import_transcripts.generate_transcript_title",
                    side_effect=lambda text, *_: ({}, f"Generated {text}", ()),
                ) as generation,
                redirect_stdout(io.StringIO()) as output,
            ):
                result = main([
                    "--data-directory", str(data), "--regenerate-titles-and-tags",
                    "--transcript-ids", str(ids[2]), str(ids[0]),
                ])

            self.assertEqual(result, 0)
            self.assertIn("2/2 stored transcript(s)", output.getvalue())
            self.assertEqual(
                [call.args[0] for call in generation.call_args_list],
                ["Text 3", "Text 1"],
            )
            by_id = {summary.id: summary for summary in store.list_for_user(111)}
            self.assertEqual(by_id[ids[0]].title, "Generated Text 1")
            self.assertEqual(by_id[ids[2]].title, "Generated Text 3")
            self.assertEqual(by_id[ids[1]].title, "Original 2")
            self.assertEqual([item.id for item in by_id[ids[1]].tags], [tag.id])

    def test_selected_ids_are_validated_before_inference(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory)
            store = TranscriptStore(data / "vorec.sqlite3")
            store.initialize()
            record = TranscriptRecord(
                telegram_user_id=111, telegram_chat_id=222, telegram_message_id=1,
                created_at="2026-09-22T10:20:30+00:00", title="Original",
                text="Text", source_audio_path="voices/1.ogg",
                artifacts_dir="transcripts/1",
            )
            store.save(record)
            record_id = store.list_all_records_with_ids()[0][0]
            with patch("vorec.import_transcripts.generate_transcript_title") as generation:
                for ids in ((0,), (record_id, record_id), (record_id, 999)):
                    with self.subTest(ids=ids), self.assertRaises(TranscriptStorageError):
                        regenerate_titles_and_tags(store, Mock(), "test-model", ids)
            generation.assert_not_called()
            self.assertEqual(store.list_for_user(111)[0].title, "Original")
            with self.assertRaises(SystemExit):
                main(["--data-directory", str(data), "--transcript-ids", str(record_id)])
