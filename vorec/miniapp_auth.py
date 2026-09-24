"""Verify Telegram Mini App initData before identifying a user."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


INIT_DATA_MAX_AGE_SECONDS = 3600
INIT_DATA_FUTURE_SKEW_SECONDS = 60


class InvalidInitData(ValueError):
    """The Telegram Mini App identity cannot be trusted."""


def verify_init_data(
    raw: str, bot_token: str, allowed_user_ids: set[int], *, now: float | None = None
) -> int:
    """Return the signed Telegram user ID, rejecting stale or malformed data."""
    if not raw or re.search(r"%(?![0-9a-fA-F]{2})", raw):
        raise InvalidInitData("Malformed initData.")
    try:
        pairs = parse_qsl(
            raw,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeError, ValueError) as error:
        raise InvalidInitData("Malformed initData.") from error
    fields: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in fields:
            raise InvalidInitData("Duplicate or empty initData field.")
        fields[key] = value
    supplied_hash = fields.get("hash", "")
    if re.fullmatch(r"[0-9a-fA-F]{64}", supplied_hash) is None:
        raise InvalidInitData("Invalid initData hash.")

    check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(pairs) if key != "hash"
    )
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(
        secret, check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash.lower()):
        raise InvalidInitData("Invalid initData signature.")

    try:
        auth_date = int(fields["auth_date"])
        user = json.loads(fields["user"])
        user_id = user["id"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidInitData("Missing Telegram user or authentication date.") from error
    current_time = time.time() if now is None else now
    if (
        auth_date < current_time - INIT_DATA_MAX_AGE_SECONDS
        or auth_date > current_time + INIT_DATA_FUTURE_SKEW_SECONDS
    ):
        raise InvalidInitData("Expired initData.")
    if type(user_id) is not int or user_id not in allowed_user_ids:
        raise InvalidInitData("Unauthorized Telegram user.")
    return user_id
