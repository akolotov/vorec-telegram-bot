"""Initialize one user's empty tag catalog from the bundled defaults."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from vorec.storage import TranscriptStorageError, TranscriptStore, normalize_tag_name


DEFAULT_TAGS_PATH = Path(__file__).with_name("default_tags.json")


class TagImportError(RuntimeError):
    """The default tags cannot be imported safely."""


def load_default_tags() -> tuple[tuple[str, str], ...]:
    """Validate the bundled tag catalog before changing a database."""
    try:
        document = json.loads(DEFAULT_TAGS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TagImportError("Could not read the bundled tag catalog.") from error

    if not isinstance(document, dict) or set(document) != {"tags"}:
        raise TagImportError("The bundled tag catalog has an invalid format.")
    entries = document["tags"]
    if not isinstance(entries, list) or not entries:
        raise TagImportError("The bundled tag catalog has no tags.")

    tags: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "description"}:
            raise TagImportError("The bundled tag catalog has an invalid entry.")
        name, description = entry["name"], entry["description"]
        if (
            not isinstance(name, str)
            or not isinstance(description, str)
            or not name.strip()
            or not description.strip()
            or name in seen
        ):
            raise TagImportError("The bundled tag catalog has an invalid tag.")
        try:
            canonical_name = normalize_tag_name(name)
        except ValueError as error:
            raise TagImportError("The bundled tag catalog has an invalid tag.") from error
        if canonical_name != name:
            raise TagImportError("The bundled tag catalog has an invalid tag.")
        seen.add(name)
        tags.append((name, description.strip()))
    return tuple(tags)


def import_default_tags(database: Path, user_id: int) -> int:
    """Insert all default tags for one user, refusing nonempty catalogs."""
    if user_id <= 0:
        raise TagImportError("The Telegram user ID must be positive.")
    if not database.is_file():
        raise TagImportError(f"Database does not exist: {database}")
    tags = load_default_tags()
    database_uri = database.resolve().as_uri()

    try:
        with sqlite3.connect(f"{database_uri}?mode=ro", uri=True) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "transcripts" not in tables:
                raise TagImportError("The database is not initialized for Vorec.")
            if "tags" in tables and connection.execute(
                "SELECT 1 FROM tags LIMIT 1"
            ).fetchone() is not None:
                raise TagImportError("The database already contains tags.")
            owners = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT telegram_user_id FROM transcripts"
                )
            }
            if owners and user_id not in owners:
                raise TagImportError(
                    "The Telegram user ID does not own any existing transcripts."
                )

        TranscriptStore(database).initialize()
        with sqlite3.connect(f"{database_uri}?mode=rw", uri=True) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM tags LIMIT 1").fetchone() is not None:
                raise TagImportError("The database already contains tags.")
            owners = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT telegram_user_id FROM transcripts"
                )
            }
            if owners and user_id not in owners:
                raise TagImportError(
                    "The Telegram user ID does not own any existing transcripts."
                )
            for name, description in tags:
                tag_id = connection.execute(
                    "INSERT INTO tag_id_allocations DEFAULT VALUES"
                ).lastrowid
                connection.execute(
                    "INSERT INTO tags (id, telegram_user_id, name, description) "
                    "VALUES (?, ?, ?, ?)",
                    (tag_id, user_id, name, description),
                )
    except TagImportError:
        raise
    except (OSError, sqlite3.Error, TranscriptStorageError) as error:
        raise TagImportError("Could not import default tags into the database.") from error
    return len(tags)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import the bundled default tags into an existing empty Vorec database."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--user-id", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        count = import_default_tags(arguments.database, arguments.user_id)
    except TagImportError as error:
        parser.error(str(error))
    print(f"Imported {count} default tags.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
