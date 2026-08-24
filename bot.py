"""Telegram bot that transcribes Russian voice messages and audio files."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from vorec.audio import (
    merge_transcripts,
    prepare_wav,
    resolve_converter,
    whisper_transcribe,
)


ENV_FILE = Path(".env")
RICH_MESSAGE_CHUNK_SIZE = 32000
TEXT_MESSAGE_CHUNK_SIZE = 3500
DEFAULT_PRIMARY_TRANSCRIPTION_MODEL = "whisper-large-v3-turbo-asr-fp16"
DEFAULT_MERGE_MODEL = "gemma-4-26b-a4b-it-4bit"
DEFAULT_GIGAAM_MODEL = "whisper-1"
DEFAULT_CONVERTER = "ffmpeg"
UNSUPPORTED_MESSAGE_TEXT = "This message type is not supported. Please send audio."
DEFAULT_WEBHOOK_LISTEN = "0.0.0.0"
DEFAULT_WEBHOOK_PORT = 8080

LOGGER = logging.getLogger(__name__)


class ConfigurationError(ValueError):
    """Raised when the bot configuration is incomplete or invalid."""


class TranscriptionError(RuntimeError):
    """A transcription failure with a user-safe English explanation."""


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


def transcript_chunks(text: str, limit: int = RICH_MESSAGE_CHUNK_SIZE) -> list[str]:
    """Split text on natural boundaries while staying below Telegram's limit."""
    text = text.strip()
    if not text:
        raise TranscriptionError("The transcription service returned an empty transcript.")

    chunks: list[str] = []
    while len(text) > limit:
        split_at = max(text.rfind("\n", 0, limit + 1), text.rfind(" ", 0, limit + 1))
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    if text:
        chunks.append(text)
    return chunks


async def send_rich_transcript_reply(message, bot, transcript: str) -> None:
    """Reply with Rich Messages, falling back to regular Telegram messages."""
    rich_messages_available = True
    for rich_chunk in transcript_chunks(transcript):
        if rich_messages_available:
            data = {
                "chat_id": message.chat_id,
                "rich_message": {
                    "blocks": [{"type": "paragraph", "text": rich_chunk}],
                    "skip_entity_detection": True,
                },
                "reply_parameters": {"message_id": message.message_id},
            }
            if message.message_thread_id is not None:
                data["message_thread_id"] = message.message_thread_id
            try:
                # TODO: Replace this private raw Bot API call with Bot.send_rich_message()
                # and the library's InputRichMessage types once python-telegram-bot supports them.
                await bot._post("sendRichMessage", data=data)
                LOGGER.info("Sent Rich Message reply with %d characters.", len(rich_chunk))
                continue
            except Exception as error:
                # PTB 22.x has no typed Rich Message API. Keep the bot useful if Telegram
                # rejects the raw method or PTB changes its private request interface.
                rich_messages_available = False
                LOGGER.warning(
                    "Rich Message delivery failed (%s); using regular messages.",
                    error.__class__.__name__,
                )

        for text_chunk in transcript_chunks(rich_chunk, limit=TEXT_MESSAGE_CHUNK_SIZE):
            await message.reply_text(text_chunk, reply_to_message_id=message.message_id)


def transcribe_recording(
    source: Path,
    gigaam_client: OpenAI,
    gigaam_model: str,
    inference_client: OpenAI,
    primary_transcription_model: str,
    merge_model: str,
    converter: str,
) -> str:
    """Run both ASR engines and consolidate their results into one transcript."""
    wav_path = source.parent / "prepared.wav"
    pipeline_started = time.monotonic()
    try:
        try:
            stage_started = time.monotonic()
            LOGGER.info("Converting audio to mono 16 kHz WAV.")
            prepare_wav(source, wav_path, converter, overwrite=True)
            LOGGER.info("Audio conversion completed in %.1f s.", time.monotonic() - stage_started)
        except (subprocess.CalledProcessError, OSError) as error:
            raise TranscriptionError(f"The audio could not be converted: {error}") from error

        try:
            stage_started = time.monotonic()
            LOGGER.info("Starting primary transcription through the inference provider.")
            primary_result = whisper_transcribe(
                wav_path, inference_client, primary_transcription_model
            )
            primary_text = primary_result.get("text")
            if not isinstance(primary_text, str) or not primary_text.strip():
                raise ValueError("the service returned empty text")
            LOGGER.info("Primary transcription completed in %.1f s.", time.monotonic() - stage_started)
        except (OpenAIError, OSError, ValueError) as error:
            raise TranscriptionError(f"The primary provider could not transcribe the audio: {error}") from error

        try:
            stage_started = time.monotonic()
            LOGGER.info("Starting GigaAM secondary transcription.")
            gigaam_result = whisper_transcribe(wav_path, gigaam_client, gigaam_model)
            gigaam_text = gigaam_result.get("text")
            if not isinstance(gigaam_text, str) or not gigaam_text.strip():
                raise ValueError("the service returned empty text")
            LOGGER.info("GigaAM transcription completed in %.1f s.", time.monotonic() - stage_started)
        except (OpenAIError, OSError, ValueError) as error:
            raise TranscriptionError(f"GigaAM could not transcribe the audio: {error}") from error

        try:
            stage_started = time.monotonic()
            LOGGER.info("Starting transcript merge through the inference provider.")
            _, merged_text = merge_transcripts(
                primary_text, gigaam_text, inference_client, merge_model
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


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    allowed_ids: set[int] = context.application.bot_data["allowed_user_ids"]
    if message is None or user is None or user.id not in allowed_ids:
        return

    attachment = message.voice or message.audio or message.document
    if attachment is None:
        return

    try:
        with tempfile.TemporaryDirectory(prefix="vorec-") as temporary_directory:
            source = Path(temporary_directory) / f"audio{attachment_suffix(message)}"
            try:
                LOGGER.info("Downloading Telegram audio.")
                telegram_file = await attachment.get_file()
                await telegram_file.download_to_drive(custom_path=source)
                LOGGER.info("Downloaded Telegram audio (%d bytes).", source.stat().st_size)
            except Exception as error:
                raise TranscriptionError(f"The audio could not be downloaded: {error}") from error

            async with context.application.bot_data["transcription_lock"]:
                # MLX models and their streams must be used from the thread that loaded them.
                transcript = transcribe_recording(
                    source,
                    context.application.bot_data["gigaam_client"],
                    context.application.bot_data["gigaam_model"],
                    context.application.bot_data["inference_client"],
                    context.application.bot_data["primary_transcription_model"],
                    context.application.bot_data["merge_model"],
                    context.application.bot_data["converter"],
                )

        await send_rich_transcript_reply(message, context.bot, transcript)
    except Exception as error:
        LOGGER.exception("Failed to process audio from Telegram user %s", user.id)
        reason = (
            str(error).strip()
            if isinstance(error, TranscriptionError)
            else f"An unexpected internal error occurred ({error.__class__.__name__})."
        )
        error_text = f"Could not transcribe the audio: {reason}"
        # Keep enough headroom for HTML entity expansion as well as the <i> tags.
        escaped_text = html.escape(error_text[:600])
        await message.reply_text(
            f"<i>{escaped_text}</i>",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.message_id,
        )


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
    app = ApplicationBuilder().token(token).build()
    app.bot_data.update(
        allowed_user_ids=user_ids,
        transcription_lock=asyncio.Lock(),
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
