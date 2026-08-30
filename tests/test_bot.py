import asyncio
import os
import threading
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from bot import (
    ConfigurationError,
    DELIVERY_FAILED_TEXT,
    DELIVERY_UNCONFIRMED_TEXT,
    FIRST_TRANSCRIPT_STATUS,
    MERGING_STATUS,
    PREPARING_STATUS,
    SECOND_TRANSCRIPT_STATUS,
    StageLocks,
    TranscriptionError,
    UNSUPPORTED_MESSAGE_TEXT,
    WAITING_FOR_FIRST_TRANSCRIPT_STATUS,
    WAITING_FOR_MERGE_STATUS,
    WAITING_FOR_PREPARATION_STATUS,
    allowed_user_ids,
    deliver_transcript,
    edit_rich_transcript_message,
    handle_audio,
    handle_unsupported_message,
    recording_paths,
    run_serial_stage,
    send_rich_transcript_reply,
    transcribe_recording,
    webhook_configuration,
)
from telegram.error import BadRequest, TimedOut
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

    def test_edits_status_into_rich_message(self) -> None:
        status_message = Mock(chat_id=123, message_id=789)
        bot = Mock()
        bot._post = AsyncMock()

        asyncio.run(edit_rich_transcript_message(status_message, bot, "final text"))

        bot._post.assert_awaited_once_with(
            "editMessageText",
            data={
                "chat_id": 123,
                "message_id": 789,
                "rich_message": {
                    "blocks": [{"type": "paragraph", "text": "final text"}],
                    "skip_entity_detection": True,
                },
            },
        )


class TranscriptDeliveryTests(unittest.TestCase):
    def test_sends_reply_when_status_was_not_created(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        bot = Mock(_post=AsyncMock())

        asyncio.run(deliver_transcript(message, None, bot, "final text"))

        bot._post.assert_awaited_once()
        self.assertEqual(bot._post.await_args.args[0], "sendRichMessage")

    def test_failed_delivery_without_status_sends_delivery_error_reply(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        message.reply_text = AsyncMock()
        bot = Mock(_post=AsyncMock(side_effect=BadRequest("send rejected")))

        asyncio.run(deliver_transcript(message, None, bot, "final text"))

        bot._post.assert_awaited_once()
        message.reply_text.assert_awaited_once_with(
            f"<i>{DELIVERY_FAILED_TEXT}</i>",
            parse_mode="HTML",
            reply_to_message_id=456,
        )

    def test_definitive_edit_failure_uses_one_fallback_reply(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        status = Mock(chat_id=123, message_id=789)
        status.edit_text = AsyncMock()
        bot = Mock()
        bot._post = AsyncMock(side_effect=[BadRequest("rejected"), True])

        asyncio.run(deliver_transcript(message, status, bot, "final text"))

        self.assertEqual(bot._post.await_count, 2)
        self.assertEqual(bot._post.await_args_list[0].args[0], "editMessageText")
        self.assertEqual(bot._post.await_args_list[1].args[0], "sendRichMessage")
        status.edit_text.assert_awaited_once_with(
            "<i>Transcription complete. The result is in the next message.</i>",
            parse_mode="HTML",
        )

    def test_both_delivery_methods_fail_without_transcription_error(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        status = Mock(chat_id=123, message_id=789)
        status.edit_text = AsyncMock()
        bot = Mock()
        bot._post = AsyncMock(
            side_effect=[BadRequest("edit rejected"), BadRequest("send rejected")]
        )

        asyncio.run(deliver_transcript(message, status, bot, "final text"))

        self.assertEqual(bot._post.await_count, 2)
        status.edit_text.assert_awaited_once_with(
            f"<i>{DELIVERY_FAILED_TEXT}</i>",
            parse_mode="HTML",
        )
        message.reply_text.assert_not_called()

    def test_uncertain_edit_is_not_retried(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        status = Mock(chat_id=123, message_id=789)
        status.edit_text = AsyncMock()
        bot = Mock(_post=AsyncMock(side_effect=TimedOut("unknown outcome")))

        asyncio.run(deliver_transcript(message, status, bot, "final text"))

        bot._post.assert_awaited_once()
        status.edit_text.assert_not_awaited()

    def test_uncertain_fallback_is_not_retried(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        status = Mock(chat_id=123, message_id=789)
        status.edit_text = AsyncMock()
        bot = Mock()
        bot._post = AsyncMock(
            side_effect=[BadRequest("edit rejected"), TimedOut("unknown outcome")]
        )

        asyncio.run(deliver_transcript(message, status, bot, "final text"))

        self.assertEqual(bot._post.await_count, 2)
        status.edit_text.assert_awaited_once_with(
            f"<i>{DELIVERY_UNCONFIRMED_TEXT}</i>",
            parse_mode="HTML",
        )


class HandleAudioTests(unittest.TestCase):
    def audio_request(self, source: Path, status_result):
        telegram_file = Mock()
        telegram_file.download_to_drive = AsyncMock(
            side_effect=lambda custom_path: Path(custom_path).touch()
        )
        attachment = Mock(file_name="memo.ogg")
        attachment.get_file = AsyncMock(return_value=telegram_file)
        message = Mock(
            chat_id=123,
            message_id=456,
            message_thread_id=None,
            voice=attachment,
            audio=None,
            document=None,
        )
        if isinstance(status_result, BaseException):
            message.reply_text = AsyncMock(side_effect=status_result)
        else:
            message.reply_text = AsyncMock(return_value=status_result)
        update = Mock(effective_message=message, effective_user=Mock(id=123))
        bot = Mock(_post=AsyncMock())
        context = Mock(bot=bot)
        context.application.bot_data = {
            "allowed_user_ids": {123},
            "transcription_lock": asyncio.Lock(),
            "stage_locks": StageLocks(),
            "gigaam_client": Mock(),
            "gigaam_model": "gigaam-model",
            "inference_client": Mock(),
            "primary_transcription_model": "whisper-model",
            "merge_model": "merge-model",
            "converter": "ffmpeg",
        }
        artifacts = source.parent / "artifacts"
        return update, context, message, bot, artifacts

    @patch("bot.transcribe_recording")
    @patch("bot.recording_paths")
    def test_updates_one_status_then_replaces_it_with_transcript(
        self, recording_paths, transcribe_recording
    ) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            status = Mock(chat_id=123, message_id=789)
            status.edit_text = AsyncMock()
            update, context, message, bot, artifacts = self.audio_request(source, status)
            recording_paths.return_value = (source, artifacts)

            async def transcribe(*args):
                progress = args[-1]
                for stage in (
                    PREPARING_STATUS,
                    FIRST_TRANSCRIPT_STATUS,
                    SECOND_TRANSCRIPT_STATUS,
                    MERGING_STATUS,
                ):
                    await progress(stage)
                return "final text"

            transcribe_recording.side_effect = transcribe
            asyncio.run(handle_audio(update, context))

        self.assertEqual(message.reply_text.await_count, 1)
        message.reply_text.assert_awaited_once_with(
            "<i>Downloading audio…</i>",
            parse_mode="HTML",
            reply_to_message_id=456,
        )
        self.assertEqual(
            [call.args[0] for call in status.edit_text.await_args_list],
            [
                f"<i>{PREPARING_STATUS}</i>",
                f"<i>{FIRST_TRANSCRIPT_STATUS}</i>",
                f"<i>{SECOND_TRANSCRIPT_STATUS}</i>",
                f"<i>{MERGING_STATUS}</i>",
            ],
        )
        bot._post.assert_awaited_once()
        self.assertEqual(bot._post.await_args.args[0], "editMessageText")

    @patch("bot.transcribe_recording", new_callable=AsyncMock, return_value="final text")
    @patch("bot.recording_paths")
    def test_status_creation_failure_still_delivers_successful_transcript(
        self, recording_paths, transcribe_recording
    ) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            update, context, message, bot, artifacts = self.audio_request(
                source, RuntimeError("status unavailable")
            )
            recording_paths.return_value = (source, artifacts)

            asyncio.run(handle_audio(update, context))

        transcribe_recording.assert_awaited_once()
        bot._post.assert_awaited_once()
        self.assertEqual(bot._post.await_args.args[0], "sendRichMessage")

    @patch(
        "bot.transcribe_recording",
        new_callable=AsyncMock,
        side_effect=TranscriptionError("provider failed"),
    )
    @patch("bot.recording_paths")
    def test_processing_failure_replaces_existing_status(
        self, recording_paths, transcribe_recording
    ) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            status = Mock(chat_id=123, message_id=789)
            status.edit_text = AsyncMock()
            update, context, message, bot, artifacts = self.audio_request(source, status)
            recording_paths.return_value = (source, artifacts)

            asyncio.run(handle_audio(update, context))

        status.edit_text.assert_awaited_once_with(
            "<i>Could not transcribe the audio: provider failed</i>",
            parse_mode="HTML",
        )
        bot._post.assert_not_awaited()

    @patch("bot.transcribe_recording")
    @patch("bot.recording_paths")
    def test_intermediate_status_failure_does_not_stop_processing(
        self, recording_paths, transcribe_recording
    ) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            status = Mock(chat_id=123, message_id=789)
            status.edit_text = AsyncMock(
                side_effect=[RuntimeError("edit failed"), None, None, None]
            )
            update, context, message, bot, artifacts = self.audio_request(source, status)
            recording_paths.return_value = (source, artifacts)

            async def transcribe(*args):
                progress = args[-1]
                for stage in (
                    PREPARING_STATUS,
                    FIRST_TRANSCRIPT_STATUS,
                    SECOND_TRANSCRIPT_STATUS,
                    MERGING_STATUS,
                ):
                    await progress(stage)
                return "final text"

            transcribe_recording.side_effect = transcribe
            asyncio.run(handle_audio(update, context))

        self.assertEqual(status.edit_text.await_count, 4)
        bot._post.assert_awaited_once()
        self.assertEqual(bot._post.await_args.args[0], "editMessageText")


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
        stages = []

        async def progress(stage: str) -> None:
            stages.append(stage)

        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            artifacts_directory = Path(directory) / "transcripts"
            source.touch()
            result = asyncio.run(
                transcribe_recording(
                    source,
                    Mock(),
                    "gigaam-model",
                    Mock(),
                    "whisper-model",
                    "merge-model",
                    "afconvert",
                    StageLocks(),
                    artifacts_directory,
                    progress,
                )
            )

            self.assertEqual(
                {path.name for path in artifacts_directory.iterdir()},
                {"whisper.json", "whisper.txt", "gigaam.json", "gigaam.txt", "merged.json", "merged.txt"},
            )
            self.assertEqual((artifacts_directory / "merged.txt").read_text(), "merged text\n")

        self.assertEqual(result, "merged text")
        self.assertEqual(
            stages,
            [
                PREPARING_STATUS,
                FIRST_TRANSCRIPT_STATUS,
                SECOND_TRANSCRIPT_STATUS,
                MERGING_STATUS,
            ],
        )
        prepare_wav.assert_called_once()
        self.assertEqual(whisper_transcribe.call_count, 2)
        merge_transcripts.assert_called_once()

    @patch("bot.prepare_wav", side_effect=OSError("unsupported input"))
    def test_reports_conversion_failure(self, prepare_wav) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            source.touch()
            with self.assertRaisesRegex(TranscriptionError, "could not be converted"):
                asyncio.run(
                    transcribe_recording(
                        source,
                        Mock(),
                        "gigaam-model",
                        Mock(),
                        "whisper-model",
                        "merge-model",
                        "afconvert",
                        StageLocks(),
                    )
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
            asyncio.run(
                transcribe_recording(
                    source,
                    Mock(),
                    "gigaam-model",
                    Mock(),
                    "whisper-model",
                    "merge-model",
                    "afconvert",
                    StageLocks(),
                )
            )

        converted_path = prepare_wav.call_args.args[1]
        self.assertNotEqual(converted_path, source)


class StageLockTests(unittest.TestCase):
    def test_free_lock_reports_only_active_status(self) -> None:
        async def scenario() -> None:
            statuses = []

            async def progress(status: str) -> None:
                statuses.append(status)

            result = await run_serial_stage(
                asyncio.Lock(),
                WAITING_FOR_PREPARATION_STATUS,
                PREPARING_STATUS,
                lambda: "done",
                progress=progress,
            )

            self.assertEqual(result, "done")
            self.assertEqual(statuses, [PREPARING_STATUS])

        asyncio.run(scenario())

    def test_busy_lock_reports_waiting_then_active_status(self) -> None:
        async def scenario() -> None:
            lock = asyncio.Lock()
            statuses = []

            async def progress(status: str) -> None:
                statuses.append(status)

            await lock.acquire()
            task = asyncio.create_task(
                run_serial_stage(
                    lock,
                    WAITING_FOR_PREPARATION_STATUS,
                    PREPARING_STATUS,
                    lambda: "done",
                    progress=progress,
                )
            )
            await asyncio.sleep(0)
            self.assertEqual(statuses, [WAITING_FOR_PREPARATION_STATUS])

            lock.release()
            self.assertEqual(await task, "done")
            self.assertEqual(
                statuses,
                [WAITING_FOR_PREPARATION_STATUS, PREPARING_STATUS],
            )

        asyncio.run(scenario())

    def test_omlx_lock_serializes_whisper_and_merge(self) -> None:
        async def scenario() -> None:
            stage_locks = StageLocks()
            order = []
            first_started = asyncio.Event()
            release_first = threading.Event()
            loop = asyncio.get_running_loop()

            def whisper() -> str:
                order.append("whisper-start")
                loop.call_soon_threadsafe(first_started.set)
                release_first.wait()
                order.append("whisper-finish")
                return "whisper"

            def merge() -> str:
                order.append("merge-start")
                return "merge"

            whisper_task = asyncio.create_task(
                run_serial_stage(
                    stage_locks.omlx,
                    WAITING_FOR_FIRST_TRANSCRIPT_STATUS,
                    FIRST_TRANSCRIPT_STATUS,
                    whisper,
                )
            )
            await first_started.wait()
            merge_task = asyncio.create_task(
                run_serial_stage(
                    stage_locks.omlx,
                    WAITING_FOR_MERGE_STATUS,
                    MERGING_STATUS,
                    merge,
                )
            )
            await asyncio.sleep(0)
            self.assertEqual(order, ["whisper-start"])

            release_first.set()
            self.assertEqual(await whisper_task, "whisper")
            self.assertEqual(await merge_task, "merge")
            self.assertEqual(
                order,
                ["whisper-start", "whisper-finish", "merge-start"],
            )

        asyncio.run(scenario())

    def test_different_stage_locks_can_run_concurrently(self) -> None:
        async def scenario() -> None:
            stage_locks = StageLocks()
            started = {"wav": asyncio.Event(), "gigaam": asyncio.Event()}
            release = threading.Event()
            loop = asyncio.get_running_loop()

            def operation(name: str) -> str:
                loop.call_soon_threadsafe(started[name].set)
                release.wait()
                return name

            wav_task = asyncio.create_task(
                run_serial_stage(
                    stage_locks.wav,
                    WAITING_FOR_PREPARATION_STATUS,
                    PREPARING_STATUS,
                    operation,
                    "wav",
                )
            )
            gigaam_task = asyncio.create_task(
                run_serial_stage(
                    stage_locks.gigaam,
                    WAITING_FOR_PREPARATION_STATUS,
                    SECOND_TRANSCRIPT_STATUS,
                    operation,
                    "gigaam",
                )
            )

            await asyncio.wait_for(
                asyncio.gather(started["wav"].wait(), started["gigaam"].wait()),
                timeout=1,
            )
            release.set()
            self.assertEqual(await wav_task, "wav")
            self.assertEqual(await gigaam_task, "gigaam")

        asyncio.run(scenario())

    def test_cancellation_holds_lock_until_thread_finishes(self) -> None:
        async def scenario() -> None:
            lock = asyncio.Lock()
            order = []
            first_started = asyncio.Event()
            release_first = threading.Event()
            loop = asyncio.get_running_loop()

            def first() -> str:
                order.append("first-start")
                loop.call_soon_threadsafe(first_started.set)
                release_first.wait()
                order.append("first-finish")
                return "first"

            def second() -> str:
                order.append("second-start")
                return "second"

            first_task = asyncio.create_task(
                run_serial_stage(
                    lock,
                    WAITING_FOR_PREPARATION_STATUS,
                    PREPARING_STATUS,
                    first,
                )
            )
            await first_started.wait()
            first_task.cancel()
            second_task = asyncio.create_task(
                run_serial_stage(
                    lock,
                    WAITING_FOR_PREPARATION_STATUS,
                    PREPARING_STATUS,
                    second,
                )
            )
            await asyncio.sleep(0)
            self.assertEqual(order, ["first-start"])

            release_first.set()
            with self.assertRaises(asyncio.CancelledError):
                await first_task
            self.assertEqual(await second_task, "second")
            self.assertEqual(
                order,
                ["first-start", "first-finish", "second-start"],
            )

        asyncio.run(scenario())


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
