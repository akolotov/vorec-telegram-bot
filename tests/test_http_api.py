import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

import httpx
from starlette.testclient import TestClient
from starlette.requests import Request
from telegram.error import BadRequest, NetworkError, TimedOut

from bot import (
    ApiJobLimiter,
    ApiTranscriptionJob,
    ConfigurationError,
    create_web_application,
    deliver_api_transcript,
    transcription_api_credentials,
)


class FakeApplication:
    def __init__(self) -> None:
        self.events = []
        self.tasks = []
        self.update_queue = asyncio.Queue()
        self.bot_data = {}
        self.bot = Mock()
        self.bot.set_webhook = AsyncMock(side_effect=self._set_webhook)

    async def _set_webhook(self, **kwargs) -> None:
        self.events.append("set_webhook")

    async def initialize(self) -> None:
        self.events.append("initialize")

    async def start(self) -> None:
        self.events.append("start")

    async def stop(self) -> None:
        self.events.append("stop")
        if self.tasks:
            await asyncio.gather(*self.tasks)

    async def shutdown(self) -> None:
        self.events.append("shutdown")

    def create_task(self, coroutine, *, name=None):
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.append(task)
        return task


def build_web_application(application, limiter, *, max_bytes=1024, timeout=60):
    return create_web_application(
        application,
        webhook_path="/hooks/bot/telegram/webhook",
        webhook_url="https://funnel.example/hooks/bot/telegram/webhook",
        webhook_secret_token="telegram-secret",
        api_path="/apps/bot/transcriptions",
        api_credentials={123: "api-secret", 456: "other-secret"},
        api_job_limiter=limiter,
        api_max_upload_bytes=max_bytes,
        api_upload_timeout_seconds=timeout,
    )


class ApiCredentialTests(unittest.TestCase):
    def test_parses_credentials_bound_to_allowed_users(self) -> None:
        self.assertEqual(
            transcription_api_credentials("123:first, 456:second", {123, 456}),
            {123: "first", 456: "second"},
        )

    def test_rejects_chat_outside_allowed_users(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "ALLOWED_USER_IDS"):
            transcription_api_credentials("456:secret", {123})

    def test_rejects_duplicate_tokens(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "tokens must be unique"):
            transcription_api_credentials("123:secret,456:secret", {123, 456})


class HttpApplicationTests(unittest.TestCase):
    def test_lifecycle_order_and_successful_raw_upload(self) -> None:
        application = FakeApplication()
        limiter = ApiJobLimiter(4)
        with TemporaryDirectory() as directory:
            source = Path(directory) / "recording.m4a"
            artifacts = Path(directory) / "transcript"
            web_application = build_web_application(application, limiter)
            with patch(
                "bot.api_recording_paths", return_value=(source, artifacts)
            ), patch("bot.process_api_transcription", new=AsyncMock()) as process:
                with TestClient(web_application) as client:
                    response = client.post(
                        "/apps/bot/transcriptions",
                        headers={
                            "Authorization": "Bearer api-secret",
                            "X-Telegram-Chat-Id": "123",
                            "Content-Type": "audio/mp4",
                        },
                        content=b"audio bytes",
                    )
                    self.assertEqual(response.status_code, 202)
                    self.assertTrue(response.json()["ok"])
                    self.assertTrue(response.json()["job_id"])
                    self.assertEqual(source.read_bytes(), b"audio bytes")

            self.assertEqual(
                application.events,
                ["initialize", "start", "set_webhook", "stop", "shutdown"],
            )
            process.assert_awaited_once()

    def test_rejects_unknown_token_and_cross_user_target(self) -> None:
        application = FakeApplication()
        web_application = build_web_application(application, ApiJobLimiter(4))
        with TestClient(web_application) as client:
            unknown = client.post(
                "/apps/bot/transcriptions",
                headers={
                    "Authorization": "Bearer unknown",
                    "X-Telegram-Chat-Id": "123",
                    "Content-Type": "audio/mp4",
                },
                content=b"audio",
            )
            mismatch = client.post(
                "/apps/bot/transcriptions",
                headers={
                    "Authorization": "Bearer api-secret",
                    "X-Telegram-Chat-Id": "456",
                    "Content-Type": "audio/mp4",
                },
                content=b"audio",
            )

        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(unknown.headers["www-authenticate"], "Bearer")
        self.assertEqual(mismatch.status_code, 403)

    def test_rejects_oversized_body_and_releases_slot(self) -> None:
        application = FakeApplication()
        limiter = ApiJobLimiter(1)
        web_application = build_web_application(application, limiter, max_bytes=3)
        with TestClient(web_application) as client:
            oversized = client.post(
                "/apps/bot/transcriptions",
                headers={
                    "Authorization": "Bearer api-secret",
                    "X-Telegram-Chat-Id": "123",
                    "Content-Type": "audio/ogg",
                },
                content=b"four",
            )
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(limiter._active, 0)

    def test_returns_429_before_reading_when_capacity_is_full(self) -> None:
        application = FakeApplication()
        limiter = ApiJobLimiter(1)
        limiter._active = 1
        web_application = build_web_application(application, limiter)
        with TestClient(web_application) as client:
            response = client.post(
                "/apps/bot/transcriptions",
                headers={
                    "Authorization": "Bearer api-secret",
                    "X-Telegram-Chat-Id": "123",
                    "Content-Type": "audio/ogg",
                },
                content=b"audio",
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "60")

    def test_telegram_webhook_validates_secret_and_queues_update(self) -> None:
        application = FakeApplication()
        web_application = build_web_application(application, ApiJobLimiter(4))
        update = object()
        with patch("bot.Update.de_json", return_value=update):
            with TestClient(web_application) as client:
                forbidden = client.post(
                    "/hooks/bot/telegram/webhook",
                    headers={"Content-Type": "application/json"},
                    json={"update_id": 1},
                )
                accepted = client.post(
                    "/hooks/bot/telegram/webhook",
                    headers={
                        "Content-Type": "application/json",
                        "X-Telegram-Bot-Api-Secret-Token": "telegram-secret",
                    },
                    json={"update_id": 1},
                )
                queued = application.update_queue.get_nowait()

        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(accepted.status_code, 200)
        self.assertIs(queued, update)

    def test_upload_deadline_returns_408_and_releases_slot(self) -> None:
        async def scenario() -> None:
            application = FakeApplication()
            limiter = ApiJobLimiter(1)
            with TemporaryDirectory() as directory:
                source = Path(directory) / "recording.ogg"
                artifacts = Path(directory) / "transcript"
                web_application = build_web_application(
                    application, limiter, timeout=0.001
                )
                route = next(
                    item
                    for item in web_application.routes
                    if item.path == "/apps/bot/transcriptions"
                )
                scope = {
                    "type": "http",
                    "method": "POST",
                    "path": "/apps/bot/transcriptions",
                    "headers": [
                        (b"authorization", b"Bearer api-secret"),
                        (b"x-telegram-chat-id", b"123"),
                        (b"content-type", b"audio/ogg"),
                    ],
                }

                async def slow_receive():
                    await asyncio.sleep(0.02)
                    return {"type": "http.request", "body": b"audio", "more_body": False}

                request = Request(scope, slow_receive)
                with patch(
                    "bot.api_recording_paths", return_value=(source, artifacts)
                ):
                    async with web_application.router.lifespan_context(web_application):
                        response = await route.endpoint(request)

                self.assertEqual(response.status_code, 408)
                self.assertEqual(limiter._active, 0)
                self.assertFalse(source.with_name("recording.ogg.part").exists())

        asyncio.run(scenario())

    def test_start_failure_shuts_down_initialized_application(self) -> None:
        application = FakeApplication()

        async def fail_start() -> None:
            application.events.append("start")
            raise RuntimeError("startup failed")

        application.start = fail_start
        web_application = build_web_application(application, ApiJobLimiter(4))
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            with TestClient(web_application):
                pass
        self.assertEqual(application.events, ["initialize", "start", "shutdown"])

    def test_webhook_registration_failure_stops_and_shuts_down(self) -> None:
        application = FakeApplication()

        async def fail_webhook(**kwargs) -> None:
            application.events.append("set_webhook")
            raise RuntimeError("registration failed")

        application.bot.set_webhook = AsyncMock(side_effect=fail_webhook)
        web_application = build_web_application(application, ApiJobLimiter(4))
        with self.assertRaisesRegex(RuntimeError, "registration failed"):
            with TestClient(web_application):
                pass
        self.assertEqual(
            application.events,
            ["initialize", "start", "set_webhook", "stop", "shutdown"],
        )


class ApiDeliveryTests(unittest.TestCase):
    @staticmethod
    def telegram_error(error, cause):
        error.__cause__ = cause
        return error

    def setUp(self) -> None:
        self.job = ApiTranscriptionJob(
            "job-123", 123, Path("audio.m4a"), Path("transcript")
        )

    def test_retries_only_when_request_was_not_sent(self) -> None:
        bot = Mock()
        bot._post = AsyncMock(
            side_effect=[
                self.telegram_error(
                    NetworkError("connect failed"), httpx.ConnectError("offline")
                ),
                None,
            ]
        )
        with patch("bot.asyncio.sleep", new=AsyncMock()):
            asyncio.run(
                deliver_api_transcript(
                    self.job, bot, "transcript", "summary"
                )
            )
        self.assertEqual(bot._post.await_count, 2)

    def test_does_not_retry_uncertain_delivery(self) -> None:
        bot = Mock()
        bot._post = AsyncMock(side_effect=TimedOut("unknown outcome"))
        asyncio.run(deliver_api_transcript(self.job, bot, "transcript", "summary"))
        bot._post.assert_awaited_once()

    def test_does_not_retry_telegram_rejection(self) -> None:
        bot = Mock()
        bot._post = AsyncMock(side_effect=BadRequest("rejected"))
        asyncio.run(deliver_api_transcript(self.job, bot, "transcript", "summary"))
        bot._post.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
