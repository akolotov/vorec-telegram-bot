"""Import the existing filesystem transcript archive into SQLite."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from vorec.audio import generate_title_only, generate_transcript_tags
from vorec.storage import TranscriptRecord, TranscriptStorageError, TranscriptStore


TITLE_FALLBACK_LENGTH = 50
DEFAULT_TITLE_MODEL = "gemma-4-26b-a4b-it-4bit"
CURRENT_RECORDING_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_"
    r"(?P<chat_id>-?\d+)_(?P<message_id>\d+)$"
)
LEGACY_RECORDING_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$"
)


class ArchiveImportError(RuntimeError):
    """Raised when an archive cannot be imported without guessing."""


@dataclass(frozen=True)
class ImportPlan:
    """Validated records and the unrelated audio files that will be ignored."""

    records: tuple[TranscriptRecord, ...]
    orphan_audio_count: int


def collect_records(data_directory: Path, user_id: int, chat_id: int) -> ImportPlan:
    """Validate an archive and return all records without changing the database."""
    transcripts_directory = data_directory / "transcripts"
    voices_directory = data_directory / "voices"
    if not transcripts_directory.is_dir():
        raise ArchiveImportError(
            f"Transcript directory does not exist: {transcripts_directory}"
        )
    if not voices_directory.is_dir():
        raise ArchiveImportError(f"Voice directory does not exist: {voices_directory}")

    artifact_directories = sorted(
        path for path in transcripts_directory.glob("*/*") if path.is_dir()
    )
    if not artifact_directories:
        raise ArchiveImportError("The archive contains no transcript directories.")

    records: list[TranscriptRecord] = []
    used_sources: set[Path] = set()
    for artifacts_directory in artifact_directories:
        if not (artifacts_directory / "merged.txt").exists():
            continue
        record = _record_from_directory(
            data_directory,
            artifacts_directory,
            user_id=user_id,
            chat_id=chat_id,
        )
        records.append(record)
        used_sources.add(data_directory / record.source_audio_path)

    all_sources = {
        path
        for path in voices_directory.glob("*/*")
        if path.is_file() and not path.name.endswith(".prepared.wav")
    }
    return ImportPlan(
        records=tuple(records),
        orphan_audio_count=len(all_sources - used_sources),
    )


def _record_from_directory(
    data_directory: Path,
    artifacts_directory: Path,
    *,
    user_id: int,
    chat_id: int,
) -> TranscriptRecord:
    name = artifacts_directory.name
    current_match = CURRENT_RECORDING_PATTERN.fullmatch(name)
    legacy_match = LEGACY_RECORDING_PATTERN.fullmatch(name)
    if current_match is not None:
        embedded_chat_id = int(current_match.group("chat_id"))
        if embedded_chat_id != chat_id:
            raise ArchiveImportError(
                f"Transcript directory {name!r} belongs to chat {embedded_chat_id}, "
                f"not the requested chat {chat_id}."
            )
        timestamp = current_match.group("timestamp")
        message_id: int | None = int(current_match.group("message_id"))
    elif legacy_match is not None:
        timestamp = legacy_match.group("timestamp")
        message_id = None
    else:
        raise ArchiveImportError(
            f"Unrecognized transcript directory name: {name!r}"
        )

    expected_month = timestamp[:7]
    if artifacts_directory.parent.name != expected_month:
        raise ArchiveImportError(
            f"Transcript directory {name!r} is stored under the wrong month."
        )
    try:
        created_at = (
            datetime.strptime(timestamp, "%Y-%m-%d_%H-%M-%S")
            .replace(tzinfo=timezone.utc)
            .isoformat(timespec="seconds")
        )
    except ValueError as error:
        raise ArchiveImportError(
            f"Transcript directory {name!r} contains an invalid timestamp."
        ) from error

    text = _read_nonempty(artifacts_directory / "merged.txt", "merged transcript")
    title_path = artifacts_directory / "title.txt"
    stored_title = _read_optional(title_path, "title")
    title = stored_title or text[:TITLE_FALLBACK_LENGTH]

    source_candidates = [
        path
        for path in (data_directory / "voices" / expected_month).glob(f"{name}.*")
        if path.is_file() and not path.name.endswith(".prepared.wav")
    ]
    if len(source_candidates) != 1:
        raise ArchiveImportError(
            f"Expected one source audio file for {name!r}, found {len(source_candidates)}."
        )
    source = source_candidates[0]

    return TranscriptRecord(
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        telegram_message_id=message_id,
        created_at=created_at,
        title=title,
        text=text,
        source_audio_path=source.relative_to(data_directory).as_posix(),
        artifacts_dir=artifacts_directory.relative_to(data_directory).as_posix(),
    )


def _read_nonempty(path: Path, label: str) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise ArchiveImportError(f"Could not read {label} {path}: {error}") from error
    if not text:
        raise ArchiveImportError(f"The {label} is empty: {path}")
    return text


def _read_optional(path: Path, label: str) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeError) as error:
        raise ArchiveImportError(f"Could not read {label} {path}: {error}") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import filesystem transcripts into the Vorec SQLite database."
    )
    parser.add_argument("--data-directory", type=Path, default=Path("data"))
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--chat-id", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--regenerate-titles-and-tags", action="store_true",
        help="Regenerate metadata for all or selected transcripts in the existing database.",
    )
    parser.add_argument(
        "--transcript-ids", nargs="+", type=int, metavar="ID",
        help="Database transcript IDs to regenerate (requires --regenerate-titles-and-tags).",
    )
    return parser


def regenerate_titles_and_tags(
    store: TranscriptStore,
    client: OpenAI,
    model: str,
    transcript_ids: tuple[int, ...] | None = None,
) -> tuple[int, int]:
    """Replace selected metadata, keeping titles on inference failure."""
    records_by_id = dict(store.list_all_records_with_ids())
    if transcript_ids is None:
        records = list(records_by_id.items())
    else:
        if any(record_id <= 0 for record_id in transcript_ids):
            raise TranscriptStorageError("Transcript IDs must be positive.")
        if len(transcript_ids) != len(set(transcript_ids)):
            raise TranscriptStorageError("Transcript IDs must be unique.")
        missing_ids = sorted(set(transcript_ids) - records_by_id.keys())
        if missing_ids:
            raise TranscriptStorageError(
                f"Transcript ID(s) not found: {', '.join(map(str, missing_ids))}."
            )
        records = [(record_id, records_by_id[record_id]) for record_id in transcript_ids]
    tags_by_user = {
        record.telegram_user_id: store.list_tags_for_user(record.telegram_user_id)
        for _, record in records
    }
    failures = 0
    for index, (record_id, record) in enumerate(records, start=1):
        available_tags = tags_by_user[record.telegram_user_id]
        failed = False
        title = record.title
        selected_ids: tuple[int, ...] = ()
        try:
            _, title = generate_title_only(record.text, client, model, timeout=60)
        except Exception as error:
            failed = True
            print(
                f"Title generation failed for transcript ID {record_id} "
                f"({index}/{len(records)}) ({error.__class__.__name__}); keeping its title."
            )
        try:
            _, selected_names = generate_transcript_tags(
                record.text, client, model, available_tags
            )
            selected_ids = tuple(
                tag.id for tag in available_tags if tag.name in selected_names
            )
        except Exception as error:
            failed = True
            print(
                f"Tag generation failed for transcript ID {record_id} "
                f"({index}/{len(records)}) ({error.__class__.__name__}); clearing tags."
            )
        failures += int(failed)
        store.save_with_tags(replace(record, title=title), selected_ids)
    return len(records), failures


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    database_path = arguments.data_directory / "vorec.sqlite3"

    if arguments.transcript_ids is not None and not arguments.regenerate_titles_and_tags:
        parser.error("--transcript-ids requires --regenerate-titles-and-tags.")
    if arguments.regenerate_titles_and_tags:
        if arguments.user_id is not None or arguments.chat_id is not None or arguments.dry_run:
            parser.error(
                "--regenerate-titles-and-tags cannot be combined with "
                "--user-id, --chat-id, or --dry-run."
            )
        if not database_path.is_file():
            parser.error(f"Transcript database does not exist: {database_path}")
        load_dotenv()
        inference_url = os.getenv("INFERENCE_API_URL", "").strip()
        inference_key = os.getenv("INFERENCE_API_KEY", "").strip()
        if not inference_url or not inference_key:
            parser.error("Primary inference configuration is incomplete.")
        client = OpenAI(base_url=inference_url, api_key=inference_key, timeout=600)
        model = os.getenv("TITLE_MODEL", DEFAULT_TITLE_MODEL)
        store = TranscriptStore(database_path)
        try:
            store.initialize()
            regenerated_count, failure_count = regenerate_titles_and_tags(
                store, client, model,
                tuple(arguments.transcript_ids) if arguments.transcript_ids is not None else None,
            )
        except TranscriptStorageError as error:
            parser.error(str(error))
        print(
            f"Regenerated title and tags for {regenerated_count - failure_count}/"
            f"{regenerated_count} stored transcript(s); {failure_count} generation failure(s)."
        )
        return 0

    if arguments.user_id is None or arguments.chat_id is None:
        parser.error("--user-id and --chat-id are required for archive import.")
    try:
        plan = collect_records(
            arguments.data_directory,
            user_id=arguments.user_id,
            chat_id=arguments.chat_id,
        )
        if arguments.dry_run:
            print(
                f"Validated {len(plan.records)} transcript(s); "
                f"{plan.orphan_audio_count} orphan audio file(s) will be ignored."
            )
            return 0

        store = TranscriptStore(database_path)
        store.initialize()
        store.save_many(plan.records)
    except (ArchiveImportError, TranscriptStorageError) as error:
        parser.error(str(error))

    print(
        f"Imported {len(plan.records)} transcript(s); "
        f"{plan.orphan_audio_count} orphan audio file(s) were ignored."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
