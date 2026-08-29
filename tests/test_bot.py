import asyncio
import os
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from bot import (
    ConfigurationError,
    TranscriptionError,
    UNSUPPORTED_MESSAGE_TEXT,
    allowed_user_ids,
    handle_unsupported_message,
    recording_paths,
    send_rich_transcript_reply,
    transcript_chunks,
    transcribe_recording,
    webhook_configuration,
)
from vorec.audio import prepare_wav, resolve_converter


class AllowedUserIdsTests(unittest.TestCase):
    def test_parses_comma_separated_ids(self) -> None:
        self.assertEqual(allowed_user_ids("123, 456,123"), {123, 456})

    def test_rejects_invalid_id(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "comma-separated integer IDs"):
            allowed_user_ids("123,alice")

    def test_rejects_empty_list(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "at least one user ID"):
            allowed_user_ids(" , ")


class PersistentStorageTests(unittest.TestCase):
    def test_recording_paths_match_persistent_directory_layout(self) -> None:
        attachment = Mock(file_name="memo.m4a")
        message = Mock(
            date=datetime(2026, 8, 22, 10, 0, 40),
            voice=attachment,
            audio=None,
            document=None,
        )

        audio_path, transcript_directory = recording_paths(message, Path("persistent-data"))

        self.assertEqual(
            audio_path, Path("persistent-data/voices/2026-08/2026-08-22_10-00-40.m4a")
        )
        self.assertEqual(
            transcript_directory,
            Path("persistent-data/transcripts/2026-08/2026-08-22_10-00-40"),
        )


class WebhookConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = {
            "WEBHOOK_PUBLIC_BASE_URL": "https://funnel.example.ts.net/",
            "WEBHOOK_PATH": "/hooks/vorec-telegram-bot/telegram/webhook",
            "WEBHOOK_SECRET_TOKEN": "secret-token",
        }
        self.patch = patch.dict(os.environ, self.environment, clear=False)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()

    def test_builds_public_url_and_defaults(self) -> None:
        self.assertEqual(
            webhook_configuration(),
            (
                "https://funnel.example.ts.net/hooks/vorec-telegram-bot/telegram/webhook",
                "/hooks/vorec-telegram-bot/telegram/webhook",
                "secret-token",
                8080,
                "0.0.0.0",
            ),
        )

    def test_rejects_non_https_public_url(self) -> None:
        os.environ["WEBHOOK_PUBLIC_BASE_URL"] = "http://funnel.example.ts.net"
        with self.assertRaisesRegex(ConfigurationError, "HTTPS URL"):
            webhook_configuration()

    def test_rejects_path_without_leading_slash(self) -> None:
        os.environ["WEBHOOK_PATH"] = "hooks/vorec-telegram-bot/telegram/webhook"
        with self.assertRaisesRegex(ConfigurationError, "start with"):
            webhook_configuration()


class TranscriptChunksTests(unittest.TestCase):
    def test_preserves_text_across_chunks(self) -> None:
        text = "alpha beta gamma delta"
        chunks = transcript_chunks(text, limit=12)
        self.assertEqual(chunks, ["alpha beta", "gamma delta"])
        self.assertEqual(" ".join(chunks), text)

    def test_splits_long_words_at_limit(self) -> None:
        self.assertEqual(transcript_chunks("abcdefgh", limit=3), ["abc", "def", "gh"])

    def test_rejects_empty_transcript(self) -> None:
        with self.assertRaisesRegex(TranscriptionError, "empty transcript"):
            transcript_chunks("  ")


class RichMessageTests(unittest.TestCase):
    def test_sends_rich_message_as_reply(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        bot = Mock()
        bot._post = AsyncMock()

        asyncio.run(send_rich_transcript_reply(message, bot, "verbatim _text_"))

        bot._post.assert_awaited_once_with(
            "sendRichMessage",
            data={
                "chat_id": 123,
                "rich_message": {
                    "blocks": [{"type": "paragraph", "text": "verbatim _text_"}],
                    "skip_entity_detection": True,
                },
                "reply_parameters": {"message_id": 456},
            },
        )
        message.reply_text.assert_not_called()

    def test_falls_back_to_regular_message_chunks(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        message.reply_text = AsyncMock()
        bot = Mock()
        bot._post = AsyncMock(side_effect=RuntimeError("unsupported method"))

        asyncio.run(send_rich_transcript_reply(message, bot, "a" * 4000))

        self.assertEqual(message.reply_text.await_count, 2)
        for call in message.reply_text.await_args_list:
            self.assertLessEqual(len(call.args[0]), 3500)
            self.assertEqual(call.kwargs["reply_to_message_id"], 456)


class UnsupportedMessageTests(unittest.TestCase):
    def test_replies_in_italic_to_allowed_user(self) -> None:
        message = Mock(message_id=456)
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message, effective_user=Mock(id=123))
        context = Mock()
        context.application.bot_data = {"allowed_user_ids": {123}}

        asyncio.run(handle_unsupported_message(update, context))

        message.reply_text.assert_awaited_once_with(
            f"<i>{UNSUPPORTED_MESSAGE_TEXT}</i>",
            parse_mode="HTML",
            reply_to_message_id=456,
        )

    def test_ignores_unauthorized_user(self) -> None:
        message = Mock(message_id=456)
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message, effective_user=Mock(id=999))
        context = Mock()
        context.application.bot_data = {"allowed_user_ids": {123}}

        asyncio.run(handle_unsupported_message(update, context))

        message.reply_text.assert_not_awaited()


class TranscribeRecordingTests(unittest.TestCase):
    @patch("bot.merge_transcripts", return_value=({"text": "merged text"}, " merged text "))
    @patch(
        "bot.whisper_transcribe",
        side_effect=[{"text": "whisper text"}, {"text": "gigaam text"}],
    )
    @patch("bot.prepare_wav")
    def test_runs_both_engines_and_returns_merged_text(
        self, prepare_wav, whisper_transcribe, merge_transcripts
    ) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            artifacts_directory = Path(directory) / "transcripts"
            source.touch()
            result = transcribe_recording(
                source,
                Mock(),
                "gigaam-model",
                Mock(),
                "whisper-model",
                "merge-model",
                "afconvert",
                artifacts_directory,
            )

            self.assertEqual(
                {path.name for path in artifacts_directory.iterdir()},
                {"whisper.json", "whisper.txt", "gigaam.json", "gigaam.txt", "merged.json", "merged.txt"},
            )
            self.assertEqual((artifacts_directory / "merged.txt").read_text(), "merged text\n")

        self.assertEqual(result, "merged text")
        prepare_wav.assert_called_once()
        self.assertEqual(whisper_transcribe.call_count, 2)
        merge_transcripts.assert_called_once()

    @patch("bot.prepare_wav", side_effect=OSError("unsupported input"))
    def test_reports_conversion_failure(self, prepare_wav) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            source.touch()
            with self.assertRaisesRegex(TranscriptionError, "could not be converted"):
                transcribe_recording(
                    source,
                    Mock(),
                    "gigaam-model",
                    Mock(),
                    "whisper-model",
                    "merge-model",
                    "afconvert",
                )

    @patch("bot.merge_transcripts", return_value=({}, "text"))
    @patch("bot.whisper_transcribe", return_value={"text": "text"})
    @patch("bot.prepare_wav")
    def test_uses_separate_output_when_source_is_wav(
        self, prepare_wav, whisper_transcribe, merge_transcripts
    ) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.wav"
            source.touch()
            transcribe_recording(
                source,
                Mock(),
                "gigaam-model",
                Mock(),
                "whisper-model",
                "merge-model",
                "afconvert",
            )

        converted_path = prepare_wav.call_args.args[1]
        self.assertNotEqual(converted_path, source)


class AudioConversionTests(unittest.TestCase):
    @patch("imageio_ffmpeg.get_ffmpeg_exe", return_value="/bundled/ffmpeg")
    @patch("vorec.audio.shutil.which", return_value=None)
    def test_uses_bundled_ffmpeg_when_system_ffmpeg_is_unavailable(
        self, which, get_ffmpeg_exe
    ) -> None:
        self.assertEqual(resolve_converter("ffmpeg"), "/bundled/ffmpeg")
        which.assert_called_once_with("ffmpeg")
        get_ffmpeg_exe.assert_called_once_with()

    @patch("imageio_ffmpeg.get_ffmpeg_exe")
    @patch("vorec.audio.shutil.which", return_value="/usr/local/bin/ffmpeg")
    def test_prefers_system_ffmpeg_when_available(self, which, get_ffmpeg_exe) -> None:
        self.assertEqual(resolve_converter("ffmpeg"), "ffmpeg")
        which.assert_called_once_with("ffmpeg")
        get_ffmpeg_exe.assert_not_called()

    @patch("vorec.audio.resolve_converter", return_value="ffmpeg")
    @patch("vorec.audio.subprocess.run")
    def test_uses_ffmpeg_by_default_format(self, run, resolve_converter) -> None:
        source = Path("audio.ogg")
        target = Path("prepared.wav")

        prepare_wav(source, target, "ffmpeg", overwrite=True)

        resolve_converter.assert_called_once_with("ffmpeg")
        run.assert_called_once_with(
            [
                "ffmpeg", "-y", "-i", "audio.ogg", "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", "prepared.wav",
            ],
            check=True,
        )

    @patch("vorec.audio.subprocess.run")
    def test_keeps_afconvert_compatibility(self, run) -> None:
        source = Path("audio.ogg")
        target = Path("prepared.wav")

        prepare_wav(source, target, "afconvert", overwrite=True)

        run.assert_called_once_with(
            [
                "afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", "--mix",
                "audio.ogg", "prepared.wav",
            ],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
