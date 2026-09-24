import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from vorec.import_transcripts import ArchiveImportError, collect_records, main


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

            with redirect_stdout(output):
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
