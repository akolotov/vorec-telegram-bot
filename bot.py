"""Telegram bot that transcribes Russian voice messages and audio files."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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

from vorec.audio import (
    merge_transcripts,
    prepare_wav,
    resolve_converter,
    whisper_transcribe,
)


ENV_FILE = Path(".env")
DEFAULT_PRIMARY_TRANSCRIPTION_MODEL = "whisper-large-v3-turbo-asr-fp16"
DEFAULT_MERGE_MODEL = "gemma-4-26b-a4b-it-4bit"
DEFAULT_GIGAAM_MODEL = "whisper-1"
DEFAULT_CONVERTER = "ffmpeg"
UNSUPPORTED_MESSAGE_TEXT = "This message type is not supported. Please send audio."
DOWNLOADING_STATUS = "Downloading audio…"
PREPARING_STATUS = "Preparing audio…"
FIRST_TRANSCRIPT_STATUS = "Creating the first transcript…"
SECOND_TRANSCRIPT_STATUS = "Creating the second transcript…"
MERGING_STATUS = "Merging transcripts…"
WAITING_FOR_PREPARATION_STATUS = "Waiting to prepare audio…"
WAITING_FOR_FIRST_TRANSCRIPT_STATUS = "Waiting to create the first transcript…"
WAITING_FOR_SECOND_TRANSCRIPT_STATUS = "Waiting to create the second transcript…"
WAITING_FOR_MERGE_STATUS = "Waiting to merge transcripts…"
DELIVERY_FAILED_TEXT = "The transcript was created, but it could not be delivered."
DELIVERY_UNCONFIRMED_TEXT = (
    "The transcript was created, but its delivery could not be confirmed."
)
DELIVERY_RETRY_DELAYS = (1, 2)
DEFAULT_WEBHOOK_LISTEN = "0.0.0.0"
DEFAULT_WEBHOOK_PORT = 8080
DATA_DIRECTORY = Path("data")

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


class ConfigurationError(ValueError):
    """Raised when the bot configuration is incomplete or invalid."""


class TranscriptionError(RuntimeError):
    """A transcription failure with a user-safe English explanation."""


@dataclass
class StageLocks:
    """Serialize the bot's use of each blocking transcription resource."""

    wav: asyncio.Lock = field(default_factory=asyncio.Lock)
    gigaam: asyncio.Lock = field(default_factory=asyncio.Lock)
    omlx: asyncio.Lock = field(default_factory=asyncio.Lock)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is not configured.")
    return value


def allowed_user_ids(value: str) -> set[int]:
    try:
        user_ids = {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError as error:
        raise ConfigurationError("ALLOWED_USER_IDS must contain comma-separated integer IDs.") from error
    if not user_ids:
        raise ConfigurationError("ALLOWED_USER_IDS must contain at least one user ID.")
    return user_ids


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


async def send_rich_transcript_reply(message, bot, transcript: str) -> None:
    """Send one Rich Message transcript as a reply to the source recording."""
    data = {
        "chat_id": message.chat_id,
        "rich_message": {
            "blocks": [{"type": "paragraph", "text": transcript}],
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


async def edit_rich_transcript_message(status_message, bot, transcript: str) -> None:
    """Replace a status message with one Rich Message transcript."""
    await bot._post(
        "editMessageText",
        data={
            "chat_id": status_message.chat_id,
            "message_id": status_message.message_id,
            "rich_message": {
                "blocks": [{"type": "paragraph", "text": transcript}],
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


async def deliver_transcript(message, status_message, bot, transcript: str) -> None:
    """Deliver a completed transcript without reporting delivery as processing failure."""
    if status_message is None:
        try:
            await retry_delivery_request(
                lambda: send_rich_transcript_reply(message, bot, transcript)
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
            lambda: edit_rich_transcript_message(status_message, bot, transcript)
        )
        return
    except Exception as error:
        LOGGER.exception("Could not replace status with the Rich Message transcript.")
        if delivery_outcome_is_uncertain(error):
            return

    try:
        await retry_delivery_request(
            lambda: send_rich_transcript_reply(message, bot, transcript)
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


async def transcribe_recording(
    source: Path,
    gigaam_client: OpenAI,
    gigaam_model: str,
    inference_client: OpenAI,
    primary_transcription_model: str,
    merge_model: str,
    converter: str,
    stage_locks: StageLocks,
    artifacts_directory: Path | None = None,
    progress: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Run both ASR engines and consolidate their results into one transcript."""
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
        except (subprocess.CalledProcessError, OSError) as error:
            raise TranscriptionError(f"The audio could not be converted: {error}") from error

        try:
            stage_started = time.monotonic()
            LOGGER.info("Starting primary transcription through the inference provider.")
            primary_result = await run_serial_stage(
                stage_locks.omlx,
                WAITING_FOR_FIRST_TRANSCRIPT_STATUS,
                FIRST_TRANSCRIPT_STATUS,
                whisper_transcribe,
                wav_path,
                inference_client,
                primary_transcription_model,
                progress=progress,
            )
            primary_text = primary_result.get("text")
            if not isinstance(primary_text, str) or not primary_text.strip():
                raise ValueError("the service returned empty text")
            if artifacts_directory is not None:
                save_transcription_artifact(
                    artifacts_directory, "whisper", primary_result, primary_text
                )
            LOGGER.info("Primary transcription completed in %.1f s.", time.monotonic() - stage_started)
        except (OpenAIError, OSError, ValueError) as error:
            raise TranscriptionError(f"The primary provider could not transcribe the audio: {error}") from error

        try:
            stage_started = time.monotonic()
            LOGGER.info("Starting GigaAM secondary transcription.")
            gigaam_result = await run_serial_stage(
                stage_locks.gigaam,
                WAITING_FOR_SECOND_TRANSCRIPT_STATUS,
                SECOND_TRANSCRIPT_STATUS,
                whisper_transcribe,
                wav_path,
                gigaam_client,
                gigaam_model,
                progress=progress,
            )
            gigaam_text = gigaam_result.get("text")
            if not isinstance(gigaam_text, str) or not gigaam_text.strip():
                raise ValueError("the service returned empty text")
            if artifacts_directory is not None:
                save_transcription_artifact(
                    artifacts_directory, "gigaam", gigaam_result, gigaam_text
                )
            LOGGER.info("GigaAM transcription completed in %.1f s.", time.monotonic() - stage_started)
        except (OpenAIError, OSError, ValueError) as error:
            raise TranscriptionError(f"GigaAM could not transcribe the audio: {error}") from error

        try:
            stage_started = time.monotonic()
            LOGGER.info("Starting transcript merge through the inference provider.")
            merged_result, merged_text = await run_serial_stage(
                stage_locks.omlx,
                WAITING_FOR_MERGE_STATUS,
                MERGING_STATUS,
                merge_transcripts,
                primary_text,
                gigaam_text,
                inference_client,
                merge_model,
                progress=progress,
            )
            if artifacts_directory is not None:
                save_transcription_artifact(
                    artifacts_directory, "merged", merged_result, merged_text
                )
            LOGGER.info("Transcript merge completed in %.1f s.", time.monotonic() - stage_started)
        except (OpenAIError, KeyError, OSError, ValueError) as error:
            raise TranscriptionError(f"The transcripts could not be merged: {error}") from error

        if not merged_text.strip():
            raise TranscriptionError("The transcription service returned an empty transcript.")
        LOGGER.info("Transcription pipeline completed in %.1f s.", time.monotonic() - pipeline_started)
        return merged_text.strip()
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

        transcript = await transcribe_recording(
            source,
            context.application.bot_data["gigaam_client"],
            context.application.bot_data["gigaam_model"],
            context.application.bot_data["inference_client"],
            context.application.bot_data["primary_transcription_model"],
            context.application.bot_data["merge_model"],
            context.application.bot_data["converter"],
            context.application.bot_data["stage_locks"],
            artifacts_directory,
            report_progress,
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

    await deliver_transcript(message, status_message, context.bot, transcript)


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


def main() -> None:
    load_dotenv(ENV_FILE)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx INFO logs include the full Bot API URL, which contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = required_env("TELEGRAM_BOT_TOKEN")
    user_ids = allowed_user_ids(required_env("ALLOWED_USER_IDS"))
    webhook_url, webhook_path, webhook_secret_token, webhook_port, webhook_listen = (
        webhook_configuration()
    )
    converter = resolve_converter(os.getenv("AUDIO_CONVERTER", DEFAULT_CONVERTER))
    if not shutil.which(converter):
        raise ConfigurationError(f"Audio converter executable not found: {converter}")

    gigaam_model = os.getenv("GIGAAM_MODEL", DEFAULT_GIGAAM_MODEL)
    gigaam_client = OpenAI(
        base_url=required_env("GIGAAM_URL"),
        api_key=required_env("GIGAAM_API_KEY"),
        timeout=600,
    )
    try:
        gigaam_client.models.retrieve(gigaam_model)
    except OpenAIError as error:
        raise ConfigurationError(
            f"GigaAM server is unavailable or rejected authentication: {error}"
        ) from error

    inference_client = OpenAI(
        base_url=required_env("INFERENCE_API_URL"),
        api_key=required_env("INFERENCE_API_KEY"),
        timeout=600,
    )

    audio_messages = filters.VOICE | filters.AUDIO | filters.Document.Category("audio/")
    app = ApplicationBuilder().token(token).concurrent_updates(True).build()
    app.bot_data.update(
        allowed_user_ids=user_ids,
        stage_locks=StageLocks(),
        gigaam_client=gigaam_client,
        gigaam_model=gigaam_model,
        inference_client=inference_client,
        primary_transcription_model=os.getenv(
            "PRIMARY_TRANSCRIPTION_MODEL", DEFAULT_PRIMARY_TRANSCRIPTION_MODEL
        ),
        merge_model=os.getenv("MERGE_MODEL", DEFAULT_MERGE_MODEL),
        converter=converter,
    )
    app.add_handler(MessageHandler(filters.User(user_id=user_ids) & audio_messages, handle_audio))
    app.add_handler(
        MessageHandler(
            filters.User(user_id=user_ids) & ~audio_messages,
            handle_unsupported_message,
        )
    )
    LOGGER.info(
        "Starting webhook listener on %s:%d%s for %d allowed user(s).",
        webhook_listen,
        webhook_port,
        webhook_path,
        len(user_ids),
    )
    app.run_webhook(
        listen=webhook_listen,
        port=webhook_port,
        url_path=webhook_path,
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        secret_token=webhook_secret_token,
    )


if __name__ == "__main__":
    main()
