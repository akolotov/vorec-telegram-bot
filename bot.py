"""Telegram bot that transcribes Russian voice messages and audio files."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from openai import OpenAI, OpenAIError
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
import uvicorn

from vorec.audio import (
    merge_transcripts,
    prepare_wav,
    resolve_converter,
    summarize_transcript,
    transcribe_audio,
)
from vorec.scheduling import TranscriptionResource, TranscriptionScheduler


ENV_FILE = Path(".env")
DEFAULT_PRIMARY_TRANSCRIPTION_MODEL = "whisper-large-v3-turbo-asr-fp16"
DEFAULT_MERGE_MODEL = "gemma-4-26b-a4b-it-4bit"
DEFAULT_SECONDARY_TRANSCRIPTION_MODEL = "whisper-podlodka-turbo-mlx"
DEFAULT_CONVERTER = "ffmpeg"
UNSUPPORTED_MESSAGE_TEXT = "This message type is not supported. Please send audio."
DOWNLOADING_STATUS = "Downloading audio…"
PREPARING_STATUS = "Preparing audio…"
FIRST_TRANSCRIPT_STATUS = "Creating the first transcript…"
SECOND_TRANSCRIPT_STATUS = "Creating the second transcript…"
MERGING_STATUS = "Merging transcripts…"
SUMMARY_STATUS = "Generating summary…"
WAITING_FOR_PREPARATION_STATUS = "Waiting to prepare audio…"
WAITING_FOR_FIRST_TRANSCRIPT_STATUS = "Waiting to create the first transcript…"
WAITING_FOR_SECOND_TRANSCRIPT_STATUS = "Waiting to create the second transcript…"
WAITING_FOR_MERGE_STATUS = "Waiting to merge transcripts…"
WAITING_FOR_SUMMARY_STATUS = "Waiting to generate summary…"
WAITING_FOR_TRANSCRIPTION_PROVIDER_STATUS = "Waiting for a transcription provider…"
WAITING_FOR_PRIMARY_INFERENCE_STATUS = "Waiting for the primary inference provider…"
WAITING_FOR_SECONDARY_INFERENCE_STATUS = "Waiting for the secondary inference provider…"
PRIMARY_TRANSCRIPTION_STATUS = "Transcribing with the primary model…"
SECONDARY_TRANSCRIPTION_STATUS = "Transcribing with the secondary model…"
DELIVERY_FAILED_TEXT = "The transcript was created, but it could not be delivered."
DELIVERY_UNCONFIRMED_TEXT = (
    "The transcript was created, but its delivery could not be confirmed."
)
DELIVERY_RETRY_DELAYS = (1, 2)
DEFAULT_WEBHOOK_LISTEN = "0.0.0.0"
DEFAULT_WEBHOOK_PORT = 8080
DATA_DIRECTORY = Path("data")
TRANSCRIPT_SUMMARY_LENGTH = 50
DEFAULT_TRANSCRIPTION_API_MAX_JOBS = 4
DEFAULT_TRANSCRIPTION_API_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
DEFAULT_TRANSCRIPTION_API_UPLOAD_TIMEOUT_SECONDS = 10 * 60
TRANSCRIPTION_API_RETRY_AFTER_SECONDS = 60
DOCKER_ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SUPPORTED_API_AUDIO_TYPES = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-flac": ".flac",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
}

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


class ConfigurationError(ValueError):
    """Raised when the bot configuration is incomplete or invalid."""


class TranscriptionError(RuntimeError):
    """A transcription failure with a user-safe English explanation."""


class ApiJobLimiter:
    """Atomically limit accepted API uploads and background jobs."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._active = 0
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self._active >= self.capacity:
                return False
            self._active += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            if self._active <= 0:
                raise RuntimeError("API job limiter released without a reservation")
            self._active -= 1


@dataclass(frozen=True)
class ApiTranscriptionJob:
    job_id: str
    chat_id: int
    source: Path
    artifacts_directory: Path


def rich_transcript_blocks(transcript: str, summary: str) -> list[dict[str, object]]:
    """Return a collapsed Rich Message details block for a transcript."""
    return [
        {
            "type": "details",
            "summary": summary,
            "blocks": [{"type": "paragraph", "text": transcript}],
        }
    ]


@dataclass
class StageLocks:
    """Serialize the bot's use of each blocking transcription resource."""

    wav: asyncio.Lock = field(default_factory=asyncio.Lock)
    secondary: asyncio.Lock = field(default_factory=asyncio.Lock)
    primary: asyncio.Lock = field(default_factory=asyncio.Lock)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is not configured.")
    return value


def boolean_env(name: str, default: bool = False) -> bool:
    """Return a strict true/false environment setting."""
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ConfigurationError(f"{name} must be either true or false.")


def allowed_user_ids(value: str) -> set[int]:
    try:
        user_ids = {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError as error:
        raise ConfigurationError("ALLOWED_USER_IDS must contain comma-separated integer IDs.") from error
    if not user_ids:
        raise ConfigurationError("ALLOWED_USER_IDS must contain at least one user ID.")
    return user_ids


def transcription_api_credentials(
    value: str, allowed_ids: set[int]
) -> dict[int, str]:
    """Parse comma-separated chat-id:token credentials and validate ownership."""
    credentials: dict[int, str] = {}
    tokens: set[str] = set()
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        chat_id_text, separator, token = item.partition(":")
        if not separator or not token.strip():
            raise ConfigurationError(
                "TRANSCRIPTION_API_CREDENTIALS must contain chat-id:token pairs."
            )
        try:
            chat_id = int(chat_id_text.strip())
        except ValueError as error:
            raise ConfigurationError(
                "TRANSCRIPTION_API_CREDENTIALS chat IDs must be integers."
            ) from error
        token = token.strip()
        if chat_id not in allowed_ids:
            raise ConfigurationError(
                "Every transcription API chat ID must be present in ALLOWED_USER_IDS."
            )
        if chat_id in credentials:
            raise ConfigurationError("Transcription API chat IDs must be unique.")
        if token in tokens:
            raise ConfigurationError("Transcription API tokens must be unique.")
        credentials[chat_id] = token
        tokens.add(token)
    if not credentials:
        raise ConfigurationError(
            "TRANSCRIPTION_API_CREDENTIALS must contain at least one chat-id:token pair."
        )
    return credentials


def positive_integer_env(name: str, default: int) -> int:
    """Return a positive integer environment setting."""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer.") from error
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero.")
    return value


def transcription_api_path() -> str:
    alias = required_env("WEBHOOK_DOCKER_ALIAS")
    if not DOCKER_ALIAS_PATTERN.fullmatch(alias):
        raise ConfigurationError("WEBHOOK_DOCKER_ALIAS is not a valid Docker network alias.")
    return f"/apps/{alias}/transcriptions"


def webhook_configuration() -> tuple[str, str, str, int, str]:
    """Return the public and internal settings for the Telegram webhook."""
    public_base_url = required_env("WEBHOOK_PUBLIC_BASE_URL").rstrip("/")
    path = required_env("WEBHOOK_PATH")
    secret_token = required_env("WEBHOOK_SECRET_TOKEN")
    parsed_url = urlparse(public_base_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ConfigurationError("WEBHOOK_PUBLIC_BASE_URL must be an HTTPS URL.")
    if not path.startswith("/"):
        raise ConfigurationError("WEBHOOK_PATH must start with '/'.")
    if not 1 <= len(secret_token) <= 256:
        raise ConfigurationError("WEBHOOK_SECRET_TOKEN must contain 1 to 256 characters.")

    try:
        port = int(os.getenv("WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT)))
    except ValueError as error:
        raise ConfigurationError("WEBHOOK_PORT must be an integer.") from error
    if not 1 <= port <= 65535:
        raise ConfigurationError("WEBHOOK_PORT must be between 1 and 65535.")

    return (
        f"{public_base_url}{path}",
        path,
        secret_token,
        port,
        os.getenv("WEBHOOK_LISTEN", DEFAULT_WEBHOOK_LISTEN),
    )


async def send_rich_transcript_reply(
    message, bot, transcript: str, summary: str
) -> None:
    """Send one Rich Message transcript as a reply to the source recording."""
    data = {
        "chat_id": message.chat_id,
        "rich_message": {
            "blocks": rich_transcript_blocks(transcript, summary),
            "skip_entity_detection": True,
        },
        "reply_parameters": {"message_id": message.message_id},
    }
    if message.message_thread_id is not None:
        data["message_thread_id"] = message.message_thread_id
    # TODO: Replace this private raw Bot API call with Bot.send_rich_message()
    # and the library's InputRichMessage types once python-telegram-bot supports them.
    await bot._post("sendRichMessage", data=data)
    LOGGER.info("Sent Rich Message reply with %d characters.", len(transcript))


async def send_rich_transcript(
    chat_id: int, bot, transcript: str, summary: str
) -> None:
    """Send a standalone Rich Message transcript to a Telegram chat."""
    await bot._post(
        "sendRichMessage",
        data={
            "chat_id": chat_id,
            "rich_message": {
                "blocks": rich_transcript_blocks(transcript, summary),
                "skip_entity_detection": True,
            },
        },
    )
    LOGGER.info("Sent standalone Rich Message with %d characters.", len(transcript))


async def edit_rich_transcript_message(
    status_message, bot, transcript: str, summary: str
) -> None:
    """Replace a status message with one Rich Message transcript."""
    await bot._post(
        "editMessageText",
        data={
            "chat_id": status_message.chat_id,
            "message_id": status_message.message_id,
            "rich_message": {
                "blocks": rich_transcript_blocks(transcript, summary),
                "skip_entity_detection": True,
            },
        },
    )
    LOGGER.info(
        "Replaced status with Rich Message transcript (%d characters).", len(transcript)
    )


async def edit_italic_status(status_message, text: str) -> bool:
    """Best-effort edit of a status message; return whether Telegram confirmed it."""
    try:
        await status_message.edit_text(
            f"<i>{html.escape(text)}</i>",
            parse_mode=ParseMode.HTML,
        )
        return True
    except Exception as error:
        LOGGER.warning("Status message edit failed (%s).", error.__class__.__name__)
        return False


def delivery_outcome_is_uncertain(error: Exception) -> bool:
    """Return whether a failed request may still have reached Telegram."""
    return (
        isinstance(error, (TimedOut, NetworkError))
        and not isinstance(error, BadRequest)
        and not delivery_request_was_not_sent(error)
    )


def delivery_request_was_not_sent(error: Exception) -> bool:
    """Return whether HTTPX confirms that Telegram never received the request."""
    return isinstance(
        error.__cause__,
        (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
    )


async def retry_delivery_request(operation: Callable[[], Awaitable[T]]) -> T:
    """Retry a Telegram delivery request only when it was definitely not sent."""
    for attempt, delay in enumerate((*DELIVERY_RETRY_DELAYS, None), start=1):
        try:
            return await operation()
        except Exception as error:
            if delay is None or not delivery_request_was_not_sent(error):
                raise
            cause = error.__cause__
            LOGGER.warning(
                "Telegram delivery request failed before sending (%s); "
                "retrying in %d s (attempt %d/%d).",
                cause.__class__.__name__,
                delay,
                attempt + 1,
                len(DELIVERY_RETRY_DELAYS) + 1,
            )
            await asyncio.sleep(delay)

    raise AssertionError("delivery retry loop exited unexpectedly")


async def report_delivery_failure(message, status_message, *, uncertain: bool) -> None:
    """Report a final delivery problem without attempting to resend the transcript."""
    text = DELIVERY_UNCONFIRMED_TEXT if uncertain else DELIVERY_FAILED_TEXT
    if status_message is not None and await edit_italic_status(status_message, text):
        return
    if status_message is not None:
        return
    try:
        await message.reply_text(
            f"<i>{html.escape(text)}</i>",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.message_id,
        )
    except Exception as error:
        LOGGER.warning("Delivery error reply failed (%s).", error.__class__.__name__)


async def deliver_transcript(
    message, status_message, bot, transcript: str, summary: str
) -> None:
    """Deliver a completed transcript without reporting delivery as processing failure."""
    if status_message is None:
        try:
            await retry_delivery_request(
                lambda: send_rich_transcript_reply(message, bot, transcript, summary)
            )
        except Exception as error:
            LOGGER.exception("Rich Message transcript delivery failed.")
            await report_delivery_failure(
                message,
                None,
                uncertain=delivery_outcome_is_uncertain(error),
            )
        return

    try:
        await retry_delivery_request(
            lambda: edit_rich_transcript_message(
                status_message, bot, transcript, summary
            )
        )
        return
    except Exception as error:
        LOGGER.exception("Could not replace status with the Rich Message transcript.")
        if delivery_outcome_is_uncertain(error):
            return

    try:
        await retry_delivery_request(
            lambda: send_rich_transcript_reply(message, bot, transcript, summary)
        )
    except Exception as fallback_error:
        LOGGER.exception("Fallback Rich Message transcript delivery failed.")
        await report_delivery_failure(
            message,
            status_message,
            uncertain=delivery_outcome_is_uncertain(fallback_error),
        )
        return

    await edit_italic_status(
        status_message,
        "Transcription complete. The result is in the next message.",
    )


async def deliver_api_transcript(
    job: ApiTranscriptionJob, bot, transcript: str, summary: str
) -> None:
    """Deliver an API transcript with duplicate-safe retry semantics."""
    try:
        await retry_delivery_request(
            lambda: send_rich_transcript(
                job.chat_id, bot, transcript, summary
            )
        )
    except Exception as error:
        outcome = (
            "unconfirmed" if delivery_outcome_is_uncertain(error) else "failed"
        )
        LOGGER.exception(
            "API transcript delivery %s for job %s.", outcome, job.job_id
        )


async def report_api_processing_failure(
    job: ApiTranscriptionJob, bot, error: Exception
) -> None:
    """Best-effort notification after an accepted API job fails."""
    reason = (
        str(error).strip()
        if isinstance(error, TranscriptionError)
        else f"An unexpected internal error occurred ({error.__class__.__name__})."
    )
    text = f"Could not transcribe API job {job.job_id}: {reason}"[:600]
    try:
        await retry_delivery_request(
            lambda: bot.send_message(
                chat_id=job.chat_id,
                text=f"<i>{html.escape(text)}</i>",
                parse_mode=ParseMode.HTML,
            )
        )
    except Exception as delivery_error:
        outcome = (
            "unconfirmed"
            if delivery_outcome_is_uncertain(delivery_error)
            else "failed"
        )
        LOGGER.exception(
            "API failure notification delivery %s for job %s.",
            outcome,
            job.job_id,
        )


async def run_serial_stage(
    lock: asyncio.Lock,
    waiting_status: str,
    active_status: str,
    operation: Callable[..., T],
    *args: object,
    progress: Callable[[str], Awaitable[None]] | None = None,
) -> T:
    """Run one blocking operation without overlapping work in the same stage."""
    if lock.locked() and progress is not None:
        await progress(waiting_status)

    async with lock:
        if progress is not None:
            await progress(active_status)
        return await run_blocking_operation(operation, *args)


async def run_blocking_operation(
    operation: Callable[..., T], *args: object
) -> T:
    """Run blocking work without abandoning it or its resource on cancellation."""
    completion = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(completion)
    except asyncio.CancelledError:
        while not completion.done():
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            completion.result()
        except BaseException:
            pass
        raise


async def run_scheduled_asr(
    resource: TranscriptionResource,
    wav_path: Path,
    primary_inference_client: OpenAI,
    primary_transcription_model: str,
    secondary_inference_client: OpenAI,
    secondary_transcription_model: str,
    artifacts_directory: Path | None,
    progress: Callable[[str], Awaitable[None]] | None,
) -> str:
    """Run the ASR operation selected by the smart scheduler."""
    if resource is TranscriptionResource.PRIMARY:
        active_status = PRIMARY_TRANSCRIPTION_STATUS
        client = primary_inference_client
        model = primary_transcription_model
        artifact_name = "primary"
        log_name = "Primary"
        error_prefix = "The primary provider could not transcribe the audio"
    else:
        active_status = SECONDARY_TRANSCRIPTION_STATUS
        client = secondary_inference_client
        model = secondary_transcription_model
        artifact_name = "secondary"
        log_name = "Secondary"
        error_prefix = "The secondary provider could not transcribe the audio"

    try:
        if progress is not None:
            await progress(active_status)
        stage_started = time.monotonic()
        LOGGER.info("Starting %s transcription.", log_name)
        result = await run_blocking_operation(
            transcribe_audio, wav_path, client, model
        )
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("the service returned empty text")
        if artifacts_directory is not None:
            save_transcription_artifact(
                artifacts_directory, artifact_name, result, text
            )
        LOGGER.info(
            "%s transcription completed in %.1f s.",
            log_name,
            time.monotonic() - stage_started,
        )
        return text
    except (OpenAIError, OSError, ValueError) as error:
        raise TranscriptionError(f"{error_prefix}: {error}") from error


async def transcribe_recording(
    source: Path,
    primary_inference_client: OpenAI,
    primary_transcription_model: str,
    secondary_inference_client: OpenAI,
    secondary_transcription_model: str,
    merge_model: str,
    summary_model: str,
    converter: str,
    stage_locks: StageLocks,
    artifacts_directory: Path | None = None,
    progress: Callable[[str], Awaitable[None]] | None = None,
    scheduler: TranscriptionScheduler | None = None,
) -> tuple[str, str]:
    """Run both ASR engines, consolidate their results, and summarize them."""
    wav_path = source.with_name(f"{source.stem}.prepared.wav")
    pipeline_started = time.monotonic()
    try:
        try:
            stage_started = time.monotonic()
            LOGGER.info("Converting audio to mono 16 kHz WAV.")
            await run_serial_stage(
                stage_locks.wav,
                WAITING_FOR_PREPARATION_STATUS,
                PREPARING_STATUS,
                prepare_wav,
                source,
                wav_path,
                converter,
                True,
                progress=progress,
            )
            LOGGER.info("Audio conversion completed in %.1f s.", time.monotonic() - stage_started)
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
            ValueError,
        ) as error:
            raise TranscriptionError(f"The audio could not be converted: {error}") from error

        if scheduler is None:
            try:
                stage_started = time.monotonic()
                LOGGER.info("Starting primary transcription through the inference provider.")
                primary_result = await run_serial_stage(
                    stage_locks.primary,
                    WAITING_FOR_FIRST_TRANSCRIPT_STATUS,
                    FIRST_TRANSCRIPT_STATUS,
                    transcribe_audio,
                    wav_path,
                    primary_inference_client,
                    primary_transcription_model,
                    progress=progress,
                )
                primary_text = primary_result.get("text")
                if not isinstance(primary_text, str) or not primary_text.strip():
                    raise ValueError("the service returned empty text")
                if artifacts_directory is not None:
                    save_transcription_artifact(
                        artifacts_directory, "primary", primary_result, primary_text
                    )
                LOGGER.info(
                    "Primary transcription completed in %.1f s.",
                    time.monotonic() - stage_started,
                )
            except (OpenAIError, OSError, ValueError) as error:
                raise TranscriptionError(
                    f"The primary provider could not transcribe the audio: {error}"
                ) from error

            try:
                stage_started = time.monotonic()
                LOGGER.info("Starting secondary transcription.")
                secondary_result = await run_serial_stage(
                    stage_locks.secondary,
                    WAITING_FOR_SECOND_TRANSCRIPT_STATUS,
                    SECOND_TRANSCRIPT_STATUS,
                    transcribe_audio,
                    wav_path,
                    secondary_inference_client,
                    secondary_transcription_model,
                    progress=progress,
                )
                secondary_text = secondary_result.get("text")
                if not isinstance(secondary_text, str) or not secondary_text.strip():
                    raise ValueError("the service returned empty text")
                if artifacts_directory is not None:
                    save_transcription_artifact(
                        artifacts_directory,
                        "secondary",
                        secondary_result,
                        secondary_text,
                    )
                LOGGER.info(
                    "Secondary transcription completed in %.1f s.",
                    time.monotonic() - stage_started,
                )
            except (OpenAIError, OSError, ValueError) as error:
                raise TranscriptionError(
                    f"The secondary provider could not transcribe the audio: {error}"
                ) from error
        else:
            transcripts: dict[TranscriptionResource, str] = {}
            remaining = (
                TranscriptionResource.PRIMARY,
                TranscriptionResource.SECONDARY,
            )
            for _ in range(2):
                waiting_status = (
                    WAITING_FOR_TRANSCRIPTION_PROVIDER_STATUS
                    if len(remaining) == 2
                    else (
                        WAITING_FOR_PRIMARY_INFERENCE_STATUS
                        if remaining[0] is TranscriptionResource.PRIMARY
                        else WAITING_FOR_SECONDARY_INFERENCE_STATUS
                    )
                )

                async def report_waiting(status: str = waiting_status) -> None:
                    if progress is not None:
                        await progress(status)

                async with scheduler.reserve(
                    remaining, on_wait=report_waiting
                ) as resource:
                    transcripts[resource] = await run_scheduled_asr(
                        resource,
                        wav_path,
                        primary_inference_client,
                        primary_transcription_model,
                        secondary_inference_client,
                        secondary_transcription_model,
                        artifacts_directory,
                        progress,
                    )
                remaining = tuple(item for item in remaining if item is not resource)

            primary_text = transcripts[TranscriptionResource.PRIMARY]
            secondary_text = transcripts[TranscriptionResource.SECONDARY]

        try:
            stage_started = time.monotonic()
            LOGGER.info("Starting transcript merge through the inference provider.")
            if scheduler is None:
                merged_result, merged_text = await run_serial_stage(
                    stage_locks.primary,
                    WAITING_FOR_MERGE_STATUS,
                    MERGING_STATUS,
                    merge_transcripts,
                    primary_text,
                    secondary_text,
                    primary_inference_client,
                    merge_model,
                    progress=progress,
                )
            else:
                async def report_merge_waiting() -> None:
                    if progress is not None:
                        await progress(WAITING_FOR_MERGE_STATUS)

                async with scheduler.reserve(
                    (TranscriptionResource.PRIMARY,),
                    on_wait=report_merge_waiting,
                ):
                    if progress is not None:
                        await progress(MERGING_STATUS)
                    merged_result, merged_text = await run_blocking_operation(
                        merge_transcripts,
                        primary_text,
                        secondary_text,
                        primary_inference_client,
                        merge_model,
                    )
            if artifacts_directory is not None:
                save_transcription_artifact(
                    artifacts_directory, "merged", merged_result, merged_text
                )
            LOGGER.info("Transcript merge completed in %.1f s.", time.monotonic() - stage_started)
        except (OpenAIError, KeyError, OSError, ValueError) as error:
            raise TranscriptionError(f"The transcripts could not be merged: {error}") from error

        transcript = merged_text.strip()
        if not transcript:
            raise TranscriptionError("The transcription service returned an empty transcript.")

        summary = transcript[:TRANSCRIPT_SUMMARY_LENGTH]
        try:
            stage_started = time.monotonic()
            LOGGER.info("Starting transcript summary through the inference provider.")
            if scheduler is None:
                summary_result, summary = await run_serial_stage(
                    stage_locks.primary,
                    WAITING_FOR_SUMMARY_STATUS,
                    SUMMARY_STATUS,
                    summarize_transcript,
                    transcript,
                    primary_inference_client,
                    summary_model,
                    progress=progress,
                )
            else:
                async def report_summary_waiting() -> None:
                    if progress is not None:
                        await progress(WAITING_FOR_SUMMARY_STATUS)

                async with scheduler.reserve(
                    (TranscriptionResource.PRIMARY,),
                    on_wait=report_summary_waiting,
                ):
                    if progress is not None:
                        await progress(SUMMARY_STATUS)
                    summary_result, summary = await run_blocking_operation(
                        summarize_transcript,
                        transcript,
                        primary_inference_client,
                        summary_model,
                    )
            if artifacts_directory is not None:
                save_transcription_artifact(
                    artifacts_directory, "summary", summary_result, summary
                )
            LOGGER.info(
                "Transcript summary completed in %.1f s.",
                time.monotonic() - stage_started,
            )
        except Exception:
            LOGGER.exception(
                "Transcript summary failed; using the transcript prefix instead."
            )
            summary = transcript[:TRANSCRIPT_SUMMARY_LENGTH]

        LOGGER.info("Transcription pipeline completed in %.1f s.", time.monotonic() - pipeline_started)
        return transcript, summary
    finally:
        wav_path.unlink(missing_ok=True)


def attachment_suffix(message) -> str:
    attachment = message.voice or message.audio or message.document
    file_name = getattr(attachment, "file_name", None)
    suffix = Path(file_name).suffix if file_name else ""
    if suffix:
        return suffix
    mime_type = getattr(attachment, "mime_type", None)
    return {
        "audio/aac": ".aac",
        "audio/m4a": ".m4a",
        "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
    }.get(mime_type, ".ogg")


def recording_paths(message, data_directory: Path = DATA_DIRECTORY) -> tuple[Path, Path]:
    """Return persistent audio and transcript paths for a Telegram message."""
    timestamp = message.date.strftime("%Y-%m-%d_%H-%M-%S")
    month = message.date.strftime("%Y-%m")
    recording_id = f"{timestamp}_{message.chat_id}_{message.message_id}"
    source = data_directory / "voices" / month / f"{recording_id}{attachment_suffix(message)}"
    return source, data_directory / "transcripts" / month / recording_id


def save_transcription_artifact(
    artifacts_directory: Path, name: str, response: dict, text: str
) -> None:
    """Persist one engine's complete response and extracted transcript."""
    artifacts_directory.mkdir(parents=True, exist_ok=True)
    (artifacts_directory / f"{name}.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (artifacts_directory / f"{name}.txt").write_text(text.strip() + "\n", encoding="utf-8")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    allowed_ids: set[int] = context.application.bot_data["allowed_user_ids"]
    if message is None or user is None or user.id not in allowed_ids:
        return

    attachment = message.voice or message.audio or message.document
    if attachment is None:
        return

    status_message = None
    try:
        status_message = await message.reply_text(
            f"<i>{DOWNLOADING_STATUS}</i>",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.message_id,
        )
    except Exception as error:
        LOGGER.warning("Could not create transcription status (%s).", error.__class__.__name__)

    try:
        source, artifacts_directory = recording_paths(message)
        source.parent.mkdir(parents=True, exist_ok=True)
        try:
            LOGGER.info("Downloading Telegram audio.")
            telegram_file = await attachment.get_file()
            await telegram_file.download_to_drive(custom_path=source)
            LOGGER.info("Downloaded Telegram audio (%d bytes).", source.stat().st_size)
        except Exception as error:
            raise TranscriptionError(f"The audio could not be downloaded: {error}") from error

        async def report_progress(text: str) -> None:
            if status_message is not None:
                await edit_italic_status(status_message, text)

        transcript, summary = await transcribe_recording(
            source,
            context.application.bot_data["primary_inference_client"],
            context.application.bot_data["primary_transcription_model"],
            context.application.bot_data["secondary_inference_client"],
            context.application.bot_data["secondary_transcription_model"],
            context.application.bot_data["merge_model"],
            context.application.bot_data["summary_model"],
            context.application.bot_data["converter"],
            context.application.bot_data["stage_locks"],
            artifacts_directory,
            report_progress,
            scheduler=context.application.bot_data["transcription_scheduler"],
        )
    except Exception as error:
        LOGGER.exception("Failed to process audio from Telegram user %s", user.id)
        reason = (
            str(error).strip()
            if isinstance(error, TranscriptionError)
            else f"An unexpected internal error occurred ({error.__class__.__name__})."
        )
        error_text = f"Could not transcribe the audio: {reason}"
        if status_message is not None and await edit_italic_status(
            status_message, error_text[:600]
        ):
            return
        try:
            await message.reply_text(
                f"<i>{html.escape(error_text[:600])}</i>",
                parse_mode=ParseMode.HTML,
                reply_to_message_id=message.message_id,
            )
        except Exception as reply_error:
            LOGGER.warning(
                "Processing error reply failed (%s).", reply_error.__class__.__name__
            )
        return

    await deliver_transcript(message, status_message, context.bot, transcript, summary)


async def handle_unsupported_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Tell an allowed user that the bot accepts only audio messages."""
    message = update.effective_message
    user = update.effective_user
    allowed_ids: set[int] = context.application.bot_data["allowed_user_ids"]
    if message is None or user is None or user.id not in allowed_ids:
        return

    await message.reply_text(
        f"<i>{UNSUPPORTED_MESSAGE_TEXT}</i>",
        parse_mode=ParseMode.HTML,
        reply_to_message_id=message.message_id,
    )


def api_recording_paths(
    job_id: str,
    suffix: str,
    *,
    data_directory: Path = DATA_DIRECTORY,
    now: datetime | None = None,
) -> tuple[Path, Path]:
    """Return persistent paths for an API-submitted recording."""
    timestamp = now or datetime.now(UTC)
    month = timestamp.strftime("%Y-%m")
    recording_id = f"{timestamp.strftime('%Y-%m-%d_%H-%M-%S')}_api_{job_id}"
    source = data_directory / "voices" / month / f"{recording_id}{suffix}"
    return source, data_directory / "transcripts" / month / recording_id


def bearer_chat_id(request: Request, credentials: dict[int, str]) -> int | None:
    """Return the chat ID owned by the request's Bearer token."""
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        return None
    for chat_id, expected_token in credentials.items():
        if secrets.compare_digest(token, expected_token):
            return chat_id
    return None


async def process_api_transcription(application, job: ApiTranscriptionJob) -> None:
    """Run the shared pipeline and deliver one accepted API job."""
    try:
        transcript, summary = await transcribe_recording(
            job.source,
            application.bot_data["primary_inference_client"],
            application.bot_data["primary_transcription_model"],
            application.bot_data["secondary_inference_client"],
            application.bot_data["secondary_transcription_model"],
            application.bot_data["merge_model"],
            application.bot_data["summary_model"],
            application.bot_data["converter"],
            application.bot_data["stage_locks"],
            job.artifacts_directory,
            scheduler=application.bot_data["transcription_scheduler"],
        )
    except Exception as error:
        LOGGER.exception("API transcription failed for job %s.", job.job_id)
        await report_api_processing_failure(job, application.bot, error)
        return

    await deliver_api_transcript(job, application.bot, transcript, summary)


def create_web_application(
    application,
    *,
    webhook_path: str,
    webhook_url: str,
    webhook_secret_token: str,
    api_path: str,
    api_credentials: dict[int, str],
    api_job_limiter: ApiJobLimiter,
    api_max_upload_bytes: int,
    api_upload_timeout_seconds: int,
) -> Starlette:
    """Build the shared Telegram webhook and transcription API server."""
    accepting_api_requests = False

    async def telegram_webhook(request: Request) -> Response:
        if request.headers.get("content-type", "").partition(";")[0] != "application/json":
            return JSONResponse(
                {"ok": False, "error": "Expected application/json."},
                status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
        supplied_secret = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not secrets.compare_digest(supplied_secret, webhook_secret_token):
            return JSONResponse(
                {"ok": False, "error": "Forbidden."},
                status_code=HTTPStatus.FORBIDDEN,
            )
        try:
            data = await request.json()
            update = Update.de_json(data, application.bot)
        except Exception:
            LOGGER.exception("Could not deserialize a Telegram webhook update.")
            return JSONResponse(
                {"ok": False, "error": "Invalid Telegram update."},
                status_code=HTTPStatus.BAD_REQUEST,
            )
        if update is not None:
            await application.update_queue.put(update)
        return Response(status_code=HTTPStatus.OK)

    async def transcription_api(request: Request) -> Response:
        nonlocal accepting_api_requests
        if not accepting_api_requests:
            return JSONResponse(
                {"ok": False, "error": "Service is not accepting new jobs."},
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            )

        token_chat_id = bearer_chat_id(request, api_credentials)
        if token_chat_id is None:
            return JSONResponse(
                {"ok": False, "error": "Invalid Bearer token."},
                status_code=HTTPStatus.UNAUTHORIZED,
                headers={"WWW-Authenticate": "Bearer"},
            )

        chat_id_header = request.headers.get("x-telegram-chat-id", "")
        try:
            chat_id = int(chat_id_header)
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "X-Telegram-Chat-Id must be an integer."},
                status_code=HTTPStatus.BAD_REQUEST,
            )
        if chat_id != token_chat_id:
            return JSONResponse(
                {"ok": False, "error": "The token does not authorize this chat."},
                status_code=HTTPStatus.FORBIDDEN,
            )

        content_type = request.headers.get("content-type", "").partition(";")[0].lower()
        suffix = SUPPORTED_API_AUDIO_TYPES.get(content_type)
        if suffix is None:
            return JSONResponse(
                {"ok": False, "error": "Unsupported audio Content-Type."},
                status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                return JSONResponse(
                    {"ok": False, "error": "Invalid Content-Length."},
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            if declared_size < 0:
                return JSONResponse(
                    {"ok": False, "error": "Invalid Content-Length."},
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            if declared_size > api_max_upload_bytes:
                return JSONResponse(
                    {"ok": False, "error": "Audio body is too large."},
                    status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )

        if not await api_job_limiter.try_acquire():
            return JSONResponse(
                {"ok": False, "error": "Too many transcription jobs."},
                status_code=HTTPStatus.TOO_MANY_REQUESTS,
                headers={"Retry-After": str(TRANSCRIPTION_API_RETRY_AFTER_SECONDS)},
            )

        reservation_transferred = False
        job_id = uuid.uuid4().hex
        source, artifacts_directory = api_recording_paths(job_id, suffix)
        partial_source = source.with_name(f"{source.name}.part")
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            bytes_received = 0
            try:
                async with asyncio.timeout(api_upload_timeout_seconds):
                    with partial_source.open("xb") as output:
                        async for chunk in request.stream():
                            bytes_received += len(chunk)
                            if bytes_received > api_max_upload_bytes:
                                return JSONResponse(
                                    {"ok": False, "error": "Audio body is too large."},
                                    status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                )
                            output.write(chunk)
            except TimeoutError:
                return JSONResponse(
                    {"ok": False, "error": "Audio upload timed out."},
                    status_code=HTTPStatus.REQUEST_TIMEOUT,
                )
            except ClientDisconnect:
                return Response(status_code=HTTPStatus.BAD_REQUEST)

            if bytes_received == 0:
                return JSONResponse(
                    {"ok": False, "error": "Audio body is empty."},
                    status_code=HTTPStatus.BAD_REQUEST,
                )

            partial_source.replace(source)
            job = ApiTranscriptionJob(job_id, chat_id, source, artifacts_directory)

            async def run_reserved_job() -> None:
                try:
                    await process_api_transcription(application, job)
                finally:
                    await api_job_limiter.release()

            job_coroutine = run_reserved_job()
            try:
                application.create_task(
                    job_coroutine, name=f"api-transcription:{job_id}"
                )
            except Exception:
                job_coroutine.close()
                source.unlink(missing_ok=True)
                LOGGER.exception("Could not schedule API transcription job %s.", job_id)
                return JSONResponse(
                    {"ok": False, "error": "Could not schedule the transcription."},
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            reservation_transferred = True
            LOGGER.info(
                "Accepted API transcription job %s for chat %d (%d bytes).",
                job_id,
                chat_id,
                bytes_received,
            )
            return JSONResponse(
                {"ok": True, "job_id": job_id},
                status_code=HTTPStatus.ACCEPTED,
            )
        except OSError:
            LOGGER.exception("Could not persist API transcription job %s.", job_id)
            return JSONResponse(
                {"ok": False, "error": "Could not store the audio."},
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        finally:
            partial_source.unlink(missing_ok=True)
            if not reservation_transferred:
                await api_job_limiter.release()

    @asynccontextmanager
    async def lifespan(_: Starlette):
        nonlocal accepting_api_requests
        initialized = False
        started = False
        try:
            await application.initialize()
            initialized = True
            await application.start()
            started = True
            await application.bot.set_webhook(
                url=webhook_url,
                allowed_updates=Update.ALL_TYPES,
                secret_token=webhook_secret_token,
            )
            accepting_api_requests = True
            yield
        finally:
            accepting_api_requests = False
            try:
                if started:
                    await application.stop()
            finally:
                if initialized:
                    await application.shutdown()

    return Starlette(
        routes=[
            Route(webhook_path, telegram_webhook, methods=["POST"]),
            Route(api_path, transcription_api, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    load_dotenv(ENV_FILE)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx INFO logs include the full Bot API URL, which contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = required_env("TELEGRAM_BOT_TOKEN")
    user_ids = allowed_user_ids(required_env("ALLOWED_USER_IDS"))
    api_credentials = transcription_api_credentials(
        required_env("TRANSCRIPTION_API_CREDENTIALS"), user_ids
    )
    api_path = transcription_api_path()
    api_max_jobs = positive_integer_env(
        "TRANSCRIPTION_API_MAX_JOBS", DEFAULT_TRANSCRIPTION_API_MAX_JOBS
    )
    api_max_upload_bytes = positive_integer_env(
        "TRANSCRIPTION_API_MAX_UPLOAD_BYTES",
        DEFAULT_TRANSCRIPTION_API_MAX_UPLOAD_BYTES,
    )
    api_upload_timeout_seconds = positive_integer_env(
        "TRANSCRIPTION_API_UPLOAD_TIMEOUT_SECONDS",
        DEFAULT_TRANSCRIPTION_API_UPLOAD_TIMEOUT_SECONDS,
    )
    webhook_url, webhook_path, webhook_secret_token, webhook_port, webhook_listen = (
        webhook_configuration()
    )
    converter = resolve_converter(DEFAULT_CONVERTER)
    smart_scheduling = boolean_env("SMART_TRANSCRIPTION_SCHEDULING")
    if not shutil.which(converter):
        raise ConfigurationError(f"Audio converter executable not found: {converter}")

    secondary_transcription_model = os.getenv(
        "SECONDARY_TRANSCRIPTION_MODEL", DEFAULT_SECONDARY_TRANSCRIPTION_MODEL
    )
    secondary_inference_client = OpenAI(
        base_url=required_env("SECONDARY_INFERENCE_API_URL"),
        api_key=required_env("SECONDARY_INFERENCE_API_KEY"),
        timeout=600,
    )

    primary_inference_client = OpenAI(
        base_url=required_env("INFERENCE_API_URL"),
        api_key=required_env("INFERENCE_API_KEY"),
        timeout=600,
    )
    scheduler = TranscriptionScheduler() if smart_scheduling else None

    audio_messages = filters.VOICE | filters.AUDIO | filters.Document.Category("audio/")
    app = (
        ApplicationBuilder()
        .token(token)
        .updater(None)
        .concurrent_updates(True)
        .build()
    )
    app.bot_data.update(
        allowed_user_ids=user_ids,
        stage_locks=StageLocks(),
        primary_inference_client=primary_inference_client,
        primary_transcription_model=os.getenv(
            "PRIMARY_TRANSCRIPTION_MODEL", DEFAULT_PRIMARY_TRANSCRIPTION_MODEL
        ),
        secondary_inference_client=secondary_inference_client,
        secondary_transcription_model=secondary_transcription_model,
        merge_model=os.getenv("MERGE_MODEL", DEFAULT_MERGE_MODEL),
        summary_model=required_env("SUMMARY_MODEL"),
        converter=converter,
        transcription_scheduler=scheduler,
    )
    app.add_handler(MessageHandler(filters.User(user_id=user_ids) & audio_messages, handle_audio))
    app.add_handler(
        MessageHandler(
            filters.User(user_id=user_ids) & ~audio_messages,
            handle_unsupported_message,
        )
    )
    web_application = create_web_application(
        app,
        webhook_path=webhook_path,
        webhook_url=webhook_url,
        webhook_secret_token=webhook_secret_token,
        api_path=api_path,
        api_credentials=api_credentials,
        api_job_limiter=ApiJobLimiter(api_max_jobs),
        api_max_upload_bytes=api_max_upload_bytes,
        api_upload_timeout_seconds=api_upload_timeout_seconds,
    )
    LOGGER.info(
        "Starting HTTP listener on %s:%d for Telegram at %s and transcription API at %s.",
        webhook_listen,
        webhook_port,
        webhook_path,
        api_path,
    )
    uvicorn.run(
        web_application,
        host=webhook_listen,
        port=webhook_port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
