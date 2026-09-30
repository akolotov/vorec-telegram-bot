"""Read-only groupings for the transcript Mini App."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from vorec.storage import TranscriptDetail, TranscriptSummary


def _instant(record: TranscriptSummary) -> datetime:
    return datetime.fromisoformat(record.created_at).astimezone(timezone.utc)


def _sorted(records: list[TranscriptSummary]) -> list[TranscriptSummary]:
    return sorted(records, key=lambda record: (_instant(record), record.id), reverse=True)


def validated_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise ValueError("Invalid timezone.") from error


def transcript_item(record: TranscriptSummary) -> dict[str, object]:
    """Return public list metadata with all assigned tags."""
    return {
        "id": record.id,
        "created_at": record.created_at,
        "title": record.title,
        "tags": [tag.name for tag in record.tags],
    }


def transcript_detail(record: TranscriptDetail) -> dict[str, object]:
    return {
        **transcript_item(record),
        "tag_ids": [tag.id for tag in record.tags],
        "text": record.text,
    }


def group_by_date(
    records: list[TranscriptSummary], timezone_name: str
) -> list[dict[str, object]]:
    """Group by a user's local calendar date, newest first."""
    local_timezone = validated_timezone(timezone_name)

    groups: dict[str, list[dict[str, object]]] = {}
    for record in _sorted(records):
        day = _instant(record).astimezone(local_timezone).date().isoformat()
        groups.setdefault(day, []).append(transcript_item(record))
    return [
        {"key": day, "tag": None, "untagged": False, "items": groups[day]}
        for day in sorted(groups, reverse=True)
    ]
