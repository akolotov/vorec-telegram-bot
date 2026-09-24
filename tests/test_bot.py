import asyncio
import os
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, call, patch

import httpx
from bot import (
    ConfigurationError,
    DEFAULT_TITLE_MODEL,
    DELIVERY_FAILED_TEXT,
    DELIVERY_UNCONFIRMED_TEXT,
    FIRST_TRANSCRIPT_STATUS,
    MERGING_STATUS,
    PREPARING_STATUS,
    PRIMARY_TRANSCRIPTION_STATUS,
    SECONDARY_TRANSCRIPTION_STATUS,
    SECOND_TRANSCRIPT_STATUS,
    STORAGE_FAILED_TEXT,
    TITLE_STATUS,
    StageLocks,
    TranscriptionError,
    UNSUPPORTED_MESSAGE_TEXT,
    WAITING_FOR_FIRST_TRANSCRIPT_STATUS,
    WAITING_FOR_MERGE_STATUS,
    WAITING_FOR_PREPARATION_STATUS,
    allowed_user_ids,
    boolean_env,
    deliver_transcript,
    edit_rich_transcript_message,
    ensure_menu_on_first_message,
    handle_audio,
    handle_unsupported_message,
    main,
    mini_app_configuration,
    recording_paths,
    required_env,
    rich_transcript_blocks,
    run_serial_stage,
    send_rich_transcript_reply,
    set_personal_menu_button,
    transcribe_recording,
    webhook_configuration,
)
from telegram.error import BadRequest, NetworkError, TimedOut
from vorec.audio import (
    TITLE_MAX_TOKENS,
    TITLE_REQUEST_TIMEOUT,
    prepare_wav,
    resolve_converter,
    generate_transcript_title,
)
from vorec.scheduling import TranscriptionScheduler
from vorec.storage import TranscriptStorageError


class AllowedUserIdsTests(unittest.TestCase):
    def test_parses_comma_separated_ids(self) -> None:
        self.assertEqual(allowed_user_ids("123, 456,123"), {123, 456})

    def test_rejects_invalid_id(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "comma-separated integer IDs"):
            allowed_user_ids("123,alice")

    def test_rejects_empty_list(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "at least one user ID"):
            allowed_user_ids(" , ")


class BooleanEnvironmentTests(unittest.TestCase):
    def test_uses_default_when_setting_is_absent(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(boolean_env("SMART_TRANSCRIPTION_SCHEDULING"))

    def test_accepts_true_and_false_case_insensitively(self) -> None:
        for value, expected in ((" TrUe ", True), ("FALSE", False)):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ,
                    {"SMART_TRANSCRIPTION_SCHEDULING": value},
                    clear=True,
                ):
                    self.assertIs(
                        boolean_env("SMART_TRANSCRIPTION_SCHEDULING"), expected
                    )

    def test_rejects_unknown_value(self) -> None:
        with patch.dict(
            os.environ,
            {"SMART_TRANSCRIPTION_SCHEDULING": "yes"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigurationError, "true or false"):
                boolean_env("SMART_TRANSCRIPTION_SCHEDULING")


class PersistentStorageTests(unittest.TestCase):
    def test_recording_paths_match_persistent_directory_layout(self) -> None:
        attachment = Mock(file_name="memo.m4a")
        message = Mock(
            date=datetime(2026, 8, 22, 10, 0, 40),
            chat_id=123,
            message_id=456,
            voice=attachment,
            audio=None,
            document=None,
        )

        audio_path, transcript_directory = recording_paths(message, Path("persistent-data"))

        self.assertEqual(
            audio_path,
            Path("persistent-data/voices/2026-08/2026-08-22_10-00-40_123_456.m4a"),
        )
        self.assertEqual(
            transcript_directory,
            Path("persistent-data/transcripts/2026-08/2026-08-22_10-00-40_123_456"),
        )

    def test_recording_paths_include_chat_id_to_avoid_cross_chat_collisions(self) -> None:
        attachment = Mock(file_name="memo.m4a")
        first_message = Mock(
            date=datetime(2026, 8, 22, 10, 0, 40),
            chat_id=123,
            message_id=456,
            voice=attachment,
            audio=None,
            document=None,
        )
        second_message = Mock(
            date=datetime(2026, 8, 22, 10, 0, 40),
            chat_id=-100987,
            message_id=456,
            voice=attachment,
            audio=None,
            document=None,
        )

        first_paths = recording_paths(first_message, Path("persistent-data"))
        second_paths = recording_paths(second_message, Path("persistent-data"))

        self.assertNotEqual(first_paths, second_paths)
        self.assertEqual(
            second_paths[0],
            Path("persistent-data/voices/2026-08/2026-08-22_10-00-40_-100987_456.m4a"),
        )

    def test_recording_paths_include_message_id_to_avoid_same_chat_collisions(self) -> None:
        attachment = Mock(file_name="memo.m4a")
        first_message = Mock(
            date=datetime(2026, 8, 22, 10, 0, 40),
            chat_id=123,
            message_id=456,
            voice=attachment,
            audio=None,
            document=None,
        )
        second_message = Mock(
            date=datetime(2026, 8, 22, 10, 0, 40),
            chat_id=123,
            message_id=457,
            voice=attachment,
            audio=None,
            document=None,
        )

        first_paths = recording_paths(first_message, Path("persistent-data"))
        second_paths = recording_paths(second_message, Path("persistent-data"))

        self.assertNotEqual(first_paths, second_paths)
        self.assertEqual(
            second_paths[1],
            Path("persistent-data/transcripts/2026-08/2026-08-22_10-00-40_123_457"),
        )


class WebhookConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = {
            "COMMON_PUBLIC_BASE_URL": "https://funnel.example.ts.net/",
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
        os.environ["COMMON_PUBLIC_BASE_URL"] = "http://funnel.example.ts.net"
        with self.assertRaisesRegex(ConfigurationError, "HTTPS origin URL"):
            webhook_configuration()

    def test_rejects_path_without_leading_slash(self) -> None:
        os.environ["WEBHOOK_PATH"] = "hooks/vorec-telegram-bot/telegram/webhook"
        with self.assertRaisesRegex(ConfigurationError, "start with"):
            webhook_configuration()


class MiniAppConfigurationTests(unittest.TestCase):
    def test_builds_url_from_shared_origin_and_path(self) -> None:
        environment = {
            "COMMON_PUBLIC_BASE_URL": "https://funnel.example.ts.net",
            "WEBHOOK_DOCKER_ALIAS": "bot-test",
            "MINI_APP_PATH": "/apps/bot-test/",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(
                mini_app_configuration(),
                ("https://funnel.example.ts.net/apps/bot-test/", "/apps/bot-test/"),
            )
            os.environ["MINI_APP_PATH"] = "/apps/other-bot/"
            with self.assertRaises(ConfigurationError):
                mini_app_configuration()

    def test_existing_base_url_and_derived_path_still_work(self) -> None:
        with patch.dict(os.environ, {
            "WEBHOOK_PUBLIC_BASE_URL": "https://funnel.example.ts.net",
            "WEBHOOK_DOCKER_ALIAS": "bot-test",
        }, clear=True):
            self.assertEqual(
                mini_app_configuration(),
                ("https://funnel.example.ts.net/apps/bot-test/", "/apps/bot-test/"),
            )

    def test_menu_is_configured_once_on_first_private_message(self) -> None:
        async def scenario():
            bot = Mock(set_chat_menu_button=AsyncMock())
            context = Mock(bot=bot)
            context.application.bot_data = {
                "mini_app_url": "https://funnel.example.ts.net/apps/bot/",
                "menu_attempted_users": set(),
            }
            update = Mock(effective_user=Mock(id=123), effective_message=Mock(chat_id=123))
            await ensure_menu_on_first_message(update, context)
            await ensure_menu_on_first_message(update, context)
            bot.set_chat_menu_button.assert_awaited_once()
            button = bot.set_chat_menu_button.await_args.kwargs["menu_button"]
            self.assertEqual(button.text, "Memos")
            self.assertEqual(button.web_app.url, "https://funnel.example.ts.net/apps/bot/")

        asyncio.run(scenario())

    def test_menu_failure_does_not_block_bot(self) -> None:
        bot = Mock(set_chat_menu_button=AsyncMock(side_effect=RuntimeError("unavailable")))
        asyncio.run(
            set_personal_menu_button(bot, 123, "https://funnel.example.ts.net/apps/bot/")
        )
        bot.set_chat_menu_button.assert_awaited_once()


class TranscriptTitleTests(unittest.TestCase):
    def test_uses_short_non_retrying_request_and_returns_clean_text(self) -> None:
        message = Mock(content="  Перенос запуска из-за ошибки оплаты  ")
        response = Mock(choices=[Mock(message=message)])
        response.model_dump.return_value = {"id": "title-response"}
        title_client = Mock()
        title_client.chat.completions.create.return_value = response
        client = Mock()
        client.with_options.return_value = title_client

        result = generate_transcript_title("Full transcript", client, "title-model")

        self.assertEqual(
            result,
            (
                {"id": "title-response"},
                "Перенос запуска из-за ошибки оплаты",
            ),
        )
        client.with_options.assert_called_once_with(
            timeout=TITLE_REQUEST_TIMEOUT,
            max_retries=0,
        )
        request = title_client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "title-model")
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(request["max_tokens"], TITLE_MAX_TOKENS)
        self.assertIn("5-10 words", request["messages"][0]["content"])
        self.assertIn("same broad topic", request["messages"][0]["content"])
        self.assertIn(
            "<TRANSCRIPT>\nFull transcript\n</TRANSCRIPT>",
            request["messages"][0]["content"],
        )

    def test_rejects_empty_title_response(self) -> None:
        response = Mock(choices=[])
        title_client = Mock()
        title_client.chat.completions.create.return_value = response
        client = Mock()
        client.with_options.return_value = title_client

        with self.assertRaisesRegex(ValueError, "empty assistant text"):
            generate_transcript_title("Full transcript", client, "title-model")

    def test_malformed_title_response_raises_parsing_error(self) -> None:
        response = Mock(choices=[object()])
        title_client = Mock()
        title_client.chat.completions.create.return_value = response
        client = Mock()
        client.with_options.return_value = title_client

        with self.assertRaises(AttributeError):
            generate_transcript_title("Full transcript", client, "title-model")


class RichMessageTests(unittest.TestCase):
    def test_uses_provided_details_title(self) -> None:
        self.assertEqual(
            rich_transcript_blocks("full transcript", "Specific title"),
            [
                {
                    "type": "details",
                    "summary": "Specific title",
                    "blocks": [{"type": "paragraph", "text": "full transcript"}],
                }
            ],
        )

    def test_sends_rich_message_as_reply(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        bot = Mock()
        bot._post = AsyncMock()

        asyncio.run(
            send_rich_transcript_reply(
                message, bot, "verbatim _text_", "Specific title"
            )
        )

        bot._post.assert_awaited_once_with(
            "sendRichMessage",
            data={
                "chat_id": 123,
                "rich_message": {
                    "blocks": [
                        {
                            "type": "details",
                            "summary": "Specific title",
                            "blocks": [
                                {"type": "paragraph", "text": "verbatim _text_"}
                            ],
                        }
                    ],
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

        asyncio.run(
            edit_rich_transcript_message(
                status_message, bot, "final text", "Specific title"
            )
        )

        bot._post.assert_awaited_once_with(
            "editMessageText",
            data={
                "chat_id": 123,
                "message_id": 789,
                "rich_message": {
                    "blocks": [
                        {
                            "type": "details",
                            "summary": "Specific title",
                            "blocks": [
                                {"type": "paragraph", "text": "final text"}
                            ],
                        }
                    ],
                    "skip_entity_detection": True,
                },
            },
        )


class TranscriptDeliveryTests(unittest.TestCase):
    @staticmethod
    def telegram_error(error, cause):
        error.__cause__ = cause
        return error

    def test_sends_reply_when_status_was_not_created(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        bot = Mock(_post=AsyncMock())

        asyncio.run(
            deliver_transcript(message, None, bot, "final text", "Specific title")
        )

        bot._post.assert_awaited_once()
        self.assertEqual(bot._post.await_args.args[0], "sendRichMessage")

    def test_failed_delivery_without_status_sends_delivery_error_reply(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        message.reply_text = AsyncMock()
        bot = Mock(_post=AsyncMock(side_effect=BadRequest("send rejected")))

        asyncio.run(
            deliver_transcript(message, None, bot, "final text", "Specific title")
        )

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

        asyncio.run(
            deliver_transcript(message, status, bot, "final text", "Specific title")
        )

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

        asyncio.run(
            deliver_transcript(message, status, bot, "final text", "Specific title")
        )

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

        asyncio.run(
            deliver_transcript(message, status, bot, "final text", "Specific title")
        )

        bot._post.assert_awaited_once()
        status.edit_text.assert_not_awaited()

    def test_connect_error_retries_edit_until_success(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        status = Mock(chat_id=123, message_id=789)
        status.edit_text = AsyncMock()
        bot = Mock()
        bot._post = AsyncMock(
            side_effect=[
                self.telegram_error(
                    NetworkError("connect failed"), httpx.ConnectError("offline")
                ),
                self.telegram_error(
                    NetworkError("connect failed"), httpx.ConnectError("offline")
                ),
                True,
            ]
        )

        with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            asyncio.run(
                deliver_transcript(
                    message, status, bot, "final text", "Specific title"
                )
            )

        self.assertEqual(bot._post.await_count, 3)
        self.assertEqual(
            [call.args[0] for call in bot._post.await_args_list],
            ["editMessageText", "editMessageText", "editMessageText"],
        )
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2])
        status.edit_text.assert_not_awaited()

    def test_exhausted_connect_error_retries_use_fallback_reply(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        status = Mock(chat_id=123, message_id=789)
        status.edit_text = AsyncMock()
        connect_errors = [
            self.telegram_error(
                NetworkError("connect failed"), httpx.ConnectError("offline")
            )
            for _ in range(3)
        ]
        bot = Mock(_post=AsyncMock(side_effect=[*connect_errors, True]))

        with patch("bot.asyncio.sleep", new_callable=AsyncMock):
            asyncio.run(
                deliver_transcript(
                    message, status, bot, "final text", "Specific title"
                )
            )

        self.assertEqual(bot._post.await_count, 4)
        self.assertEqual(
            [call.args[0] for call in bot._post.await_args_list],
            ["editMessageText", "editMessageText", "editMessageText", "sendRichMessage"],
        )
        status.edit_text.assert_awaited_once_with(
            "<i>Transcription complete. The result is in the next message.</i>",
            parse_mode="HTML",
        )

    def test_connect_error_retries_reply_when_status_was_not_created(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        message.reply_text = AsyncMock()
        bot = Mock(
            _post=AsyncMock(
                side_effect=[
                    self.telegram_error(
                        NetworkError("connect failed"), httpx.ConnectError("offline")
                    ),
                    True,
                ]
            )
        )

        with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            asyncio.run(
                deliver_transcript(
                    message, None, bot, "final text", "Specific title"
                )
            )

        self.assertEqual(bot._post.await_count, 2)
        self.assertEqual(
            [call.args[0] for call in bot._post.await_args_list],
            ["sendRichMessage", "sendRichMessage"],
        )
        sleep.assert_awaited_once_with(1)
        message.reply_text.assert_not_awaited()

    def test_connect_and_pool_timeouts_are_retried(self) -> None:
        causes = [httpx.ConnectTimeout("offline"), httpx.PoolTimeout("pool full")]
        for cause in causes:
            with self.subTest(cause=cause.__class__.__name__):
                message = Mock(chat_id=123, message_id=456, message_thread_id=None)
                message.reply_text = AsyncMock()
                bot = Mock(
                    _post=AsyncMock(
                        side_effect=[
                            self.telegram_error(TimedOut("not sent"), cause),
                            True,
                        ]
                    )
                )

                with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
                    asyncio.run(
                        deliver_transcript(
                            message, None, bot, "final text", "Specific title"
                        )
                    )

                self.assertEqual(bot._post.await_count, 2)
                sleep.assert_awaited_once_with(1)
                message.reply_text.assert_not_awaited()

    def test_read_error_is_not_retried(self) -> None:
        message = Mock(chat_id=123, message_id=456, message_thread_id=None)
        status = Mock(chat_id=123, message_id=789)
        status.edit_text = AsyncMock()
        error = self.telegram_error(
            NetworkError("read failed"), httpx.ReadError("unknown outcome")
        )
        bot = Mock(_post=AsyncMock(side_effect=error))

        asyncio.run(
            deliver_transcript(message, status, bot, "final text", "Specific title")
        )

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

        asyncio.run(
            deliver_transcript(message, status, bot, "final text", "Specific title")
        )

        self.assertEqual(bot._post.await_count, 2)
        status.edit_text.assert_awaited_once_with(
            f"<i>{DELIVERY_UNCONFIRMED_TEXT}</i>",
            parse_mode="HTML",
        )


class HandleAudioTests(unittest.TestCase):
    def audio_request(
        self,
        source: Path,
        status_result,
        *,
        user_id: int = 123,
        chat_id: int = 123,
        message_id: int = 456,
        bot=None,
    ):
        telegram_file = Mock()
        telegram_file.download_to_drive = AsyncMock(
            side_effect=lambda custom_path: Path(custom_path).touch()
        )
        attachment = Mock(file_name="memo.ogg")
        attachment.get_file = AsyncMock(return_value=telegram_file)
        message = Mock(
            chat_id=chat_id,
            message_id=message_id,
            date=datetime(2026, 8, 22, 10, 0, 40, tzinfo=timezone.utc),
            message_thread_id=None,
            voice=attachment,
            audio=None,
            document=None,
        )
        if isinstance(status_result, BaseException):
            message.reply_text = AsyncMock(side_effect=status_result)
        else:
            message.reply_text = AsyncMock(return_value=status_result)
        update = Mock(effective_message=message, effective_user=Mock(id=user_id))
        if bot is None:
            bot = Mock(_post=AsyncMock())
        context = Mock(bot=bot)
        context.application.bot_data = {
            "allowed_user_ids": {user_id},
            "stage_locks": StageLocks(),
            "primary_inference_client": Mock(),
            "primary_transcription_model": "whisper-model",
            "secondary_inference_client": Mock(),
            "secondary_transcription_model": "secondary-model",
            "merge_model": "merge-model",
            "title_model": "title-model",
            "converter": "ffmpeg",
            "transcription_scheduler": None,
            "transcript_store": Mock(),
            "data_directory": source.parent,
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

            async def transcribe(*args, **kwargs):
                progress = args[-1]
                for stage in (
                    PREPARING_STATUS,
                    FIRST_TRANSCRIPT_STATUS,
                    SECOND_TRANSCRIPT_STATUS,
                    MERGING_STATUS,
                    TITLE_STATUS,
                ):
                    await progress(stage)
                return "final text", "Specific title"

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
                f"<i>{TITLE_STATUS}</i>",
            ],
        )
        bot._post.assert_awaited_once()
        self.assertEqual(bot._post.await_args.args[0], "editMessageText")
        saved = context.application.bot_data["transcript_store"].save.call_args.args[0]
        self.assertEqual(saved.telegram_user_id, 123)
        self.assertEqual(saved.telegram_chat_id, 123)
        self.assertEqual(saved.telegram_message_id, 456)
        self.assertEqual(saved.created_at, "2026-08-22T10:00:40+00:00")
        self.assertEqual(saved.title, "Specific title")
        self.assertEqual(saved.text, "final text")
        self.assertEqual(saved.source_audio_path, "audio.ogg")
        self.assertEqual(saved.artifacts_dir, "artifacts")

    @patch(
        "bot.transcribe_recording",
        new_callable=AsyncMock,
        return_value=("final text", "Specific title"),
    )
    @patch("bot.recording_paths")
    def test_saves_transcript_before_delivery(
        self, recording_paths, transcribe_recording
    ) -> None:
        events = []
        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            status = Mock(chat_id=123, message_id=789)
            status.edit_text = AsyncMock()
            bot = Mock()

            async def post(*args, **kwargs):
                events.append("delivery")

            bot._post = AsyncMock(side_effect=post)
            update, context, _, _, artifacts = self.audio_request(
                source, status, bot=bot
            )
            context.application.bot_data["transcript_store"].save.side_effect = (
                lambda record: events.append("storage")
            )
            recording_paths.return_value = (source, artifacts)

            asyncio.run(handle_audio(update, context))

        self.assertEqual(events, ["storage", "delivery"])

    @patch(
        "bot.transcribe_recording",
        new_callable=AsyncMock,
        return_value=("final text", "Specific title"),
    )
    @patch("bot.recording_paths")
    def test_storage_failure_prevents_delivery(
        self, recording_paths, transcribe_recording
    ) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            status = Mock(chat_id=123, message_id=789)
            status.edit_text = AsyncMock()
            update, context, _, bot, artifacts = self.audio_request(source, status)
            context.application.bot_data["transcript_store"].save.side_effect = (
                TranscriptStorageError("database unavailable")
            )
            recording_paths.return_value = (source, artifacts)

            asyncio.run(handle_audio(update, context))

        status.edit_text.assert_awaited_once_with(
            f"<i>{STORAGE_FAILED_TEXT}</i>",
            parse_mode="HTML",
        )
        bot._post.assert_not_awaited()

    @patch(
        "bot.transcribe_recording",
        new_callable=AsyncMock,
        return_value=("final text", "Specific title"),
    )
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
                side_effect=[RuntimeError("edit failed"), None, None, None, None]
            )
            update, context, message, bot, artifacts = self.audio_request(source, status)
            recording_paths.return_value = (source, artifacts)

            async def transcribe(*args, **kwargs):
                progress = args[-1]
                for stage in (
                    PREPARING_STATUS,
                    FIRST_TRANSCRIPT_STATUS,
                    SECOND_TRANSCRIPT_STATUS,
                    MERGING_STATUS,
                    TITLE_STATUS,
                ):
                    await progress(stage)
                return "final text", "Specific title"

            transcribe_recording.side_effect = transcribe
            asyncio.run(handle_audio(update, context))

        self.assertEqual(status.edit_text.await_count, 5)
        bot._post.assert_awaited_once()
        self.assertEqual(bot._post.await_args.args[0], "editMessageText")

    @patch("bot.transcribe_recording")
    @patch("bot.recording_paths")
    def test_multiple_recordings_reach_transcription_concurrently(
        self, recording_paths, transcribe_recording
    ) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                first_source = Path(directory) / "first.ogg"
                second_source = Path(directory) / "second.ogg"
                first_status = Mock(chat_id=123, message_id=789)
                first_status.edit_text = AsyncMock()
                second_status = Mock(chat_id=123, message_id=790)
                second_status.edit_text = AsyncMock()
                first_update, first_context, _, _, first_artifacts = self.audio_request(
                    first_source, first_status
                )
                second_update, second_context, _, _, second_artifacts = self.audio_request(
                    second_source, second_status
                )
                second_context.application.bot_data = first_context.application.bot_data
                recording_paths.side_effect = [
                    (first_source, first_artifacts),
                    (second_source, second_artifacts),
                ]

                first_started = asyncio.Event()
                second_started = asyncio.Event()
                release = asyncio.Event()

                async def transcribe(*args, **kwargs):
                    if not first_started.is_set():
                        first_started.set()
                    else:
                        second_started.set()
                    await release.wait()
                    return "final text", "Specific title"

                transcribe_recording.side_effect = transcribe
                first_task = asyncio.create_task(handle_audio(first_update, first_context))
                await first_started.wait()
                second_task = asyncio.create_task(handle_audio(second_update, second_context))
                try:
                    await asyncio.wait_for(second_started.wait(), timeout=1)
                finally:
                    release.set()
                await asyncio.gather(first_task, second_task)

        asyncio.run(scenario())
        self.assertEqual(transcribe_recording.await_count, 2)

    @patch("bot.transcribe_recording")
    @patch("bot.recording_paths")
    def test_two_allowed_users_process_recordings_concurrently_and_reply_to_their_own_chats(
        self, recording_paths, transcribe_recording
    ) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                first_source = Path(directory) / "first.ogg"
                second_source = Path(directory) / "second.ogg"
                first_status = Mock(chat_id=10001, message_id=1010)
                first_status.edit_text = AsyncMock()
                second_status = Mock(chat_id=20002, message_id=2010)
                second_status.edit_text = AsyncMock()
                bot = Mock(_post=AsyncMock())
                first_update, first_context, _, _, first_artifacts = self.audio_request(
                    first_source,
                    first_status,
                    user_id=101,
                    chat_id=10001,
                    message_id=10,
                    bot=bot,
                )
                second_update, second_context, _, _, second_artifacts = self.audio_request(
                    second_source,
                    second_status,
                    user_id=202,
                    chat_id=20002,
                    message_id=10,
                    bot=bot,
                )
                first_context.application.bot_data["allowed_user_ids"] = {101, 202}
                second_context.application.bot_data = first_context.application.bot_data
                recording_paths.side_effect = [
                    (first_source, first_artifacts),
                    (second_source, second_artifacts),
                ]

                first_started = asyncio.Event()
                second_started = asyncio.Event()
                release = asyncio.Event()

                async def transcribe(source, *args, **kwargs):
                    if source == first_source:
                        first_started.set()
                    else:
                        second_started.set()
                    await release.wait()
                    transcript = f"transcript for {source.stem}"
                    return transcript, f"title for {source.stem}"

                transcribe_recording.side_effect = transcribe
                first_task = asyncio.create_task(handle_audio(first_update, first_context))
                await first_started.wait()
                second_task = asyncio.create_task(handle_audio(second_update, second_context))
                try:
                    await asyncio.wait_for(second_started.wait(), timeout=1)
                finally:
                    release.set()
                await asyncio.gather(first_task, second_task)

                deliveries = {
                    call.kwargs["data"]["chat_id"]: (
                        call.kwargs["data"]["message_id"],
                        call.kwargs["data"]["rich_message"]["blocks"][0]["blocks"][0][
                            "text"
                        ],
                    )
                    for call in bot._post.await_args_list
                }
                self.assertEqual(
                    deliveries,
                    {
                        10001: (1010, "transcript for first"),
                        20002: (2010, "transcript for second"),
                    },
                )

        asyncio.run(scenario())
        self.assertEqual(transcribe_recording.await_count, 2)


class ApplicationConfigurationTests(unittest.TestCase):
    @patch("bot.uvicorn.run")
    @patch("bot.create_web_application")
    @patch("bot.TranscriptStore")
    @patch("bot.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("bot.resolve_converter", return_value="ffmpeg")
    @patch("bot.load_dotenv")
    @patch("bot.OpenAI")
    @patch("bot.ApplicationBuilder")
    def test_enables_concurrent_updates(
        self,
        application_builder,
        openai,
        load_dotenv,
        resolve_converter,
        which,
        transcript_store,
        create_web_application,
        uvicorn_run,
    ) -> None:
        application = Mock()
        builder = application_builder.return_value
        builder.token.return_value.updater.return_value.concurrent_updates.return_value.build.return_value = application
        environment = {
            "TELEGRAM_BOT_TOKEN": "token",
            "ALLOWED_USER_IDS": "123",
            "COMMON_PUBLIC_BASE_URL": "https://funnel.example.ts.net",
            "WEBHOOK_PATH": "/hooks/bot/telegram/webhook",
            "WEBHOOK_DOCKER_ALIAS": "bot",
            "MINI_APP_PATH": "/apps/bot/",
            "WEBHOOK_SECRET_TOKEN": "secret-token",
            "INFERENCE_API_URL": "https://inference.example.test",
            "INFERENCE_API_KEY": "inference-key",
            "SECONDARY_INFERENCE_API_URL": "https://secondary.example.test",
            "SECONDARY_INFERENCE_API_KEY": "secondary-key",
            "SMART_TRANSCRIPTION_SCHEDULING": "false",
        }
        with patch.dict(os.environ, environment, clear=True):
            main()

        openai.assert_has_calls(
            [
                call(
                    base_url="https://secondary.example.test",
                    api_key="secondary-key",
                    timeout=600,
                ),
                call(
                    base_url="https://inference.example.test",
                    api_key="inference-key",
                    timeout=600,
                ),
            ],
            any_order=True,
        )
        builder.token.return_value.updater.return_value.concurrent_updates.assert_called_once_with(True)
        builder.token.return_value.updater.assert_called_once_with(None)
        self.assertIsNone(
            application.bot_data.update.call_args.kwargs["transcription_scheduler"]
        )
        self.assertEqual(
            application.bot_data.update.call_args.kwargs[
                "secondary_transcription_model"
            ],
            "whisper-podlodka-turbo-mlx",
        )
        self.assertEqual(
            application.bot_data.update.call_args.kwargs["title_model"],
            DEFAULT_TITLE_MODEL,
        )
        transcript_store.return_value.initialize.assert_called_once_with()
        self.assertIs(
            application.bot_data.update.call_args.kwargs["transcript_store"],
            transcript_store.return_value,
        )
        create_web_application.assert_called_once()
        uvicorn_run.assert_called_once_with(
            create_web_application.return_value,
            host="0.0.0.0",
            port=8080,
            access_log=True,
        )

    @patch("bot.uvicorn.run")
    @patch("bot.create_web_application")
    @patch("bot.TranscriptStore")
    @patch("bot.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("bot.resolve_converter", return_value="ffmpeg")
    @patch("bot.load_dotenv")
    @patch("bot.OpenAI")
    @patch("bot.ApplicationBuilder")
    def test_builds_smart_scheduler_when_enabled(
        self,
        application_builder,
        openai,
        load_dotenv,
        resolve_converter,
        which,
        transcript_store,
        create_web_application,
        uvicorn_run,
    ) -> None:
        application = Mock()
        builder = application_builder.return_value
        builder.token.return_value.updater.return_value.concurrent_updates.return_value.build.return_value = application
        environment = {
            "TELEGRAM_BOT_TOKEN": "token",
            "ALLOWED_USER_IDS": "123",
            "COMMON_PUBLIC_BASE_URL": "https://funnel.example.ts.net",
            "WEBHOOK_PATH": "/hooks/bot/telegram/webhook",
            "WEBHOOK_DOCKER_ALIAS": "bot",
            "MINI_APP_PATH": "/apps/bot/",
            "WEBHOOK_SECRET_TOKEN": "secret-token",
            "INFERENCE_API_URL": "https://inference.example.test",
            "INFERENCE_API_KEY": "inference-key",
            "SECONDARY_INFERENCE_API_URL": "https://secondary.example.test",
            "SECONDARY_INFERENCE_API_KEY": "secondary-key",
            "TITLE_MODEL": "title-model",
            "SMART_TRANSCRIPTION_SCHEDULING": "true",
        }
        with patch.dict(os.environ, environment, clear=True):
            main()

        self.assertIsInstance(
            application.bot_data.update.call_args.kwargs["transcription_scheduler"],
            TranscriptionScheduler,
        )


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
    @patch(
        "bot.generate_transcript_title",
        return_value=({"text": "Specific title"}, "Specific title"),
    )
    @patch("bot.merge_transcripts", return_value=({"text": "merged text"}, " merged text "))
    @patch(
        "bot.transcribe_audio",
        side_effect=[{"text": "primary text"}, {"text": "secondary text"}],
    )
    @patch("bot.prepare_wav")
    def test_runs_both_engines_and_returns_merged_text(
        self, prepare_wav, transcribe_audio, merge_transcripts, generate_transcript_title
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
                    "primary-model",
                    Mock(),
                    "secondary-model",
                    "merge-model",
                    "title-model",
                    "afconvert",
                    StageLocks(),
                    artifacts_directory,
                    progress,
                )
            )

            self.assertEqual(
                {path.name for path in artifacts_directory.iterdir()},
                {
                    "primary.json",
                    "primary.txt",
                    "secondary.json",
                    "secondary.txt",
                    "merged.json",
                    "merged.txt",
                    "title.json",
                    "title.txt",
                },
            )
            self.assertEqual((artifacts_directory / "merged.txt").read_text(), "merged text\n")
            self.assertEqual(
                (artifacts_directory / "title.txt").read_text(),
                "Specific title\n",
            )

        self.assertEqual(result, ("merged text", "Specific title"))
        self.assertEqual(
            stages,
            [
                PREPARING_STATUS,
                FIRST_TRANSCRIPT_STATUS,
                SECOND_TRANSCRIPT_STATUS,
                MERGING_STATUS,
                TITLE_STATUS,
            ],
        )
        prepare_wav.assert_called_once()
        self.assertEqual(transcribe_audio.call_count, 2)
        merge_transcripts.assert_called_once()
        generate_transcript_title.assert_called_once()

    def test_title_errors_fall_back_to_transcript_prefix(self) -> None:
        transcript = "a" * 50 + "hidden"
        for error in (
            AttributeError("malformed provider response"),
            RuntimeError("unexpected title failure"),
        ):
            with self.subTest(error=error.__class__.__name__):
                with TemporaryDirectory() as directory:
                    source = Path(directory) / "audio.ogg"
                    source.touch()
                    with (
                        patch("bot.prepare_wav"),
                        patch(
                            "bot.transcribe_audio",
                            side_effect=[{"text": "primary"}, {"text": "secondary"}],
                        ),
                        patch(
                            "bot.merge_transcripts",
                            return_value=({"text": transcript}, transcript),
                        ),
                        patch("bot.generate_transcript_title", side_effect=error),
                    ):
                        result = asyncio.run(
                            transcribe_recording(
                                source,
                                Mock(),
                                "primary-model",
                                Mock(),
                                "secondary-model",
                                "merge-model",
                                "title-model",
                                "afconvert",
                                StageLocks(),
                            )
                        )

                self.assertEqual(result, (transcript, "a" * 50))

    def test_title_artifact_error_uses_fallback(self) -> None:
        transcript = "a" * 50 + "hidden"

        def save_artifact(directory, name, response, text):
            if name == "title":
                raise RuntimeError("storage failed")

        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            source.touch()
            with (
                patch("bot.prepare_wav"),
                patch(
                    "bot.transcribe_audio",
                    side_effect=[{"text": "primary"}, {"text": "secondary"}],
                ),
                patch(
                    "bot.merge_transcripts",
                    return_value=({"text": transcript}, transcript),
                ),
                patch(
                    "bot.generate_transcript_title",
                    return_value=({"text": "title"}, "Specific title"),
                ),
                patch("bot.save_transcription_artifact", side_effect=save_artifact),
            ):
                result = asyncio.run(
                    transcribe_recording(
                        source,
                        Mock(),
                        "primary-model",
                        Mock(),
                        "secondary-model",
                        "merge-model",
                        "title-model",
                        "afconvert",
                        StageLocks(),
                        Path(directory) / "artifacts",
                    )
                )

        self.assertEqual(result, (transcript, "a" * 50))

    def test_title_cancellation_propagates(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            source.touch()
            with (
                patch("bot.prepare_wav"),
                patch(
                    "bot.transcribe_audio",
                    side_effect=[{"text": "primary"}, {"text": "secondary"}],
                ),
                patch(
                    "bot.merge_transcripts",
                    return_value=({"text": "merged"}, "merged"),
                ),
                patch(
                    "bot.generate_transcript_title",
                    side_effect=asyncio.CancelledError,
                ),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(
                        transcribe_recording(
                            source,
                            Mock(),
                            "primary-model",
                            Mock(),
                            "secondary-model",
                            "merge-model",
                            "title-model",
                            "afconvert",
                            StageLocks(),
                        )
                    )

    def test_smart_scheduling_starts_second_recording_on_free_secondary(self) -> None:
        async def scenario() -> None:
            scheduler = TranscriptionScheduler()
            stage_locks = StageLocks()
            started: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
            releases = {
                (recording, model): threading.Event()
                for recording in ("first", "second")
                for model in ("primary-model", "secondary-model")
            }
            loop = asyncio.get_running_loop()

            def transcribe(path, client, model):
                recording = path.name.split(".", 1)[0]
                loop.call_soon_threadsafe(started.put_nowait, (recording, model))
                if not releases[recording, model].wait(timeout=2):
                    raise TimeoutError("test transcription was not released")
                return {"text": f"{recording}-{model}"}

            with TemporaryDirectory() as directory:
                first_source = Path(directory) / "first.ogg"
                second_source = Path(directory) / "second.ogg"
                first_source.touch()
                second_source.touch()
                with (
                    patch("bot.prepare_wav"),
                    patch("bot.transcribe_audio", side_effect=transcribe),
                    patch(
                        "bot.merge_transcripts",
                        return_value=({"text": "merged"}, "merged"),
                    ),
                    patch(
                        "bot.generate_transcript_title",
                        return_value=({"text": "title"}, "title"),
                    ),
                ):
                    first = asyncio.create_task(
                        transcribe_recording(
                            first_source,
                            Mock(),
                            "primary-model",
                            Mock(),
                            "secondary-model",
                            "merge-model",
                            "title-model",
                            "afconvert",
                            stage_locks,
                            scheduler=scheduler,
                        )
                    )
                    self.assertEqual(
                        await asyncio.wait_for(started.get(), timeout=1),
                        ("first", "primary-model"),
                    )
                    second = asyncio.create_task(
                        transcribe_recording(
                            second_source,
                            Mock(),
                            "primary-model",
                            Mock(),
                            "secondary-model",
                            "merge-model",
                            "title-model",
                            "afconvert",
                            stage_locks,
                            scheduler=scheduler,
                        )
                    )
                    self.assertEqual(
                        await asyncio.wait_for(started.get(), timeout=1),
                        ("second", "secondary-model"),
                    )

                    releases["first", "primary-model"].set()
                    await asyncio.sleep(0)
                    self.assertTrue(started.empty())
                    releases["second", "secondary-model"].set()

                    swapped = {
                        await asyncio.wait_for(started.get(), timeout=1),
                        await asyncio.wait_for(started.get(), timeout=1),
                    }
                    self.assertEqual(
                        swapped,
                        {
                            ("first", "secondary-model"),
                            ("second", "primary-model"),
                        },
                    )
                    releases["first", "secondary-model"].set()
                    releases["second", "primary-model"].set()
                    self.assertEqual(
                        await asyncio.wait_for(
                            asyncio.gather(first, second), timeout=2
                        ),
                        [("merged", "title"), ("merged", "title")],
                    )

        asyncio.run(scenario())

    @patch(
        "bot.generate_transcript_title",
        return_value=({"text": "title"}, "title"),
    )
    @patch("bot.merge_transcripts", return_value=({"text": "merged"}, "merged"))
    @patch(
        "bot.transcribe_audio",
        side_effect=[{"text": "primary"}, {"text": "secondary"}],
    )
    @patch("bot.prepare_wav")
    def test_smart_scheduling_reports_actual_engine_statuses(
        self, prepare_wav, transcribe_audio, merge_transcripts, generate_transcript_title
    ) -> None:
        statuses = []

        async def progress(status: str) -> None:
            statuses.append(status)

        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            source.touch()
            result = asyncio.run(
                transcribe_recording(
                    source,
                    Mock(),
                    "primary-model",
                    Mock(),
                    "secondary-model",
                    "merge-model",
                    "title-model",
                    "afconvert",
                    StageLocks(),
                    progress=progress,
                    scheduler=TranscriptionScheduler(),
                )
            )

        self.assertEqual(result, ("merged", "title"))
        self.assertEqual(
            statuses,
            [
                PREPARING_STATUS,
                PRIMARY_TRANSCRIPTION_STATUS,
                SECONDARY_TRANSCRIPTION_STATUS,
                MERGING_STATUS,
                TITLE_STATUS,
            ],
        )

    @patch("bot.transcribe_audio", side_effect=OSError("provider failed"))
    @patch("bot.prepare_wav")
    def test_smart_scheduling_stops_after_first_asr_failure(
        self, prepare_wav, transcribe_audio
    ) -> None:
        scheduler = TranscriptionScheduler()
        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.ogg"
            source.touch()
            with self.assertRaisesRegex(
                TranscriptionError, "primary provider could not transcribe"
            ):
                asyncio.run(
                    transcribe_recording(
                        source,
                        Mock(),
                        "primary-model",
                        Mock(),
                        "secondary-model",
                        "merge-model",
                        "title-model",
                        "afconvert",
                        StageLocks(),
                        scheduler=scheduler,
                    )
                )

        transcribe_audio.assert_called_once()

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
                        "primary-model",
                        Mock(),
                        "secondary-model",
                        "merge-model",
                        "title-model",
                        "afconvert",
                        StageLocks(),
                    )
                )

    @patch("bot.merge_transcripts", return_value=({}, "text"))
    @patch("bot.transcribe_audio", return_value={"text": "text"})
    @patch("bot.prepare_wav")
    def test_uses_separate_output_when_source_is_wav(
        self, prepare_wav, transcribe_audio, merge_transcripts
    ) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "audio.wav"
            source.touch()
            asyncio.run(
                transcribe_recording(
                    source,
                    Mock(),
                    "primary-model",
                    Mock(),
                    "secondary-model",
                    "merge-model",
                    "title-model",
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

    def test_primary_lock_serializes_transcription_and_merge(self) -> None:
        async def scenario() -> None:
            stage_locks = StageLocks()
            order = []
            first_started = asyncio.Event()
            release_first = threading.Event()
            loop = asyncio.get_running_loop()

            def transcribe() -> str:
                order.append("transcribe-start")
                loop.call_soon_threadsafe(first_started.set)
                release_first.wait()
                order.append("transcribe-finish")
                return "transcript"

            def merge() -> str:
                order.append("merge-start")
                return "merge"

            transcription_task = asyncio.create_task(
                run_serial_stage(
                    stage_locks.primary,
                    WAITING_FOR_FIRST_TRANSCRIPT_STATUS,
                    FIRST_TRANSCRIPT_STATUS,
                    transcribe,
                )
            )
            await first_started.wait()
            merge_task = asyncio.create_task(
                run_serial_stage(
                    stage_locks.primary,
                    WAITING_FOR_MERGE_STATUS,
                    MERGING_STATUS,
                    merge,
                )
            )
            try:
                await asyncio.sleep(0)
                self.assertEqual(order, ["transcribe-start"])
            finally:
                release_first.set()
            self.assertEqual(await transcription_task, "transcript")
            self.assertEqual(await merge_task, "merge")
            self.assertEqual(
                order,
                ["transcribe-start", "transcribe-finish", "merge-start"],
            )

        asyncio.run(scenario())

    def test_different_stage_locks_can_run_concurrently(self) -> None:
        async def scenario() -> None:
            stage_locks = StageLocks()
            started = {"wav": asyncio.Event(), "secondary": asyncio.Event()}
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
            secondary_task = asyncio.create_task(
                run_serial_stage(
                    stage_locks.secondary,
                    WAITING_FOR_PREPARATION_STATUS,
                    SECOND_TRANSCRIPT_STATUS,
                    operation,
                    "secondary",
                )
            )

            try:
                await asyncio.wait_for(
                    asyncio.gather(started["wav"].wait(), started["secondary"].wait()),
                    timeout=1,
                )
            finally:
                release.set()
            self.assertEqual(await wav_task, "wav")
            self.assertEqual(await secondary_task, "secondary")

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
            try:
                await asyncio.sleep(0)
                self.assertEqual(order, ["first-start"])
            finally:
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
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
                "-y", "-i", "audio.ogg", "-vn", "-ac", "1", "-ar", "16000",
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
