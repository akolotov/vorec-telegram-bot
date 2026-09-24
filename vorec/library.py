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


def transcript_item(
    record: TranscriptSummary, *, excluding_tag_id: int | None = None
) -> dict[str, object]:
    """Return a public list item, hiding only its grouping tag when requested."""
    return {
        "id": record.id,
        "created_at": record.created_at,
        "title": record.title,
        "tags": [
            tag.name for tag in record.tags if tag.id != excluding_tag_id
        ],
    }


def transcript_detail(record: TranscriptDetail) -> dict[str, object]:
    return {**transcript_item(record), "text": record.text}


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


def group_by_tags(records: list[TranscriptSummary]) -> list[dict[str, object]]:
    """Assign tagged transcripts greedily, then append originally tagless ones."""
    remaining = _sorted([record for record in records if record.tags])
    groups: list[dict[str, object]] = []
    while remaining:
        candidates: dict[int, tuple[str, list[TranscriptSummary]]] = {}
        for record in remaining:
            for tag in record.tags:
                if tag.id not in candidates:
                    candidates[tag.id] = (tag.name, [])
                candidates[tag.id][1].append(record)
        tag_id, (tag_name, members) = min(
            candidates.items(),
            key=lambda candidate: (
                -len(candidate[1][1]),
                -_instant(candidate[1][1][0]).timestamp(),
                candidate[1][0],
                candidate[0],
            ),
        )
        groups.append(
            {
                "key": tag_id,
                "tag": tag_name,
                "untagged": False,
                "items": [
                    transcript_item(record, excluding_tag_id=tag_id)
                    for record in members
                ],
            }
        )
        member_ids = {record.id for record in members}
        remaining = [record for record in remaining if record.id not in member_ids]

    untagged = _sorted([record for record in records if not record.tags])
    if untagged:
        groups.append(
            {
                "key": "untagged",
                "tag": None,
                "untagged": True,
                "items": [transcript_item(record) for record in untagged],
            }
        )
    return groups
