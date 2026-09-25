import asyncio
import hashlib
import hmac
import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlencode

from starlette.testclient import TestClient

from vorec.library import group_by_date, group_by_tags
from vorec.miniapp_auth import InvalidInitData, verify_init_data
from vorec.miniapp_web import create_web_application
from vorec.storage import TagRecord, TranscriptRecord, TranscriptStore, TranscriptSummary


TOKEN = "example-bot-token"


def signed_data(fields: list[tuple[str, str]], token: str = TOKEN) -> str:
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode([*fields, ("hash", digest)])


class MiniAppAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fields = [("auth_date", "1000"), ("user", '{"id":101}')]

    def test_accepts_signature_and_any_extra_signed_field(self) -> None:
        fields = [*self.fields, ("signature", "telegram-ed25519"), ("future_key", "a+b")]
        raw = signed_data(fields)
        self.assertEqual(verify_init_data(raw, TOKEN, {101}, now=1000), 101)
        with self.assertRaises(InvalidInitData):
            verify_init_data(raw.replace("telegram-ed25519", "altered"), TOKEN, {101}, now=1000)
        with self.assertRaises(InvalidInitData):
            verify_init_data(raw.replace("future_key=", "other_key="), TOKEN, {101}, now=1000)

    def test_public_telegram_mini_apps_vector_includes_signature(self) -> None:
        # Public example from Telegram-Mini-Apps/init-data-golang, not a real credential.
        raw = (
            "user=%7B%22id%22%3A279058397%2C%22first_name%22%3A%22Vladislav%20%2B%20-%20%3F%20%5C%2F%22%2C"
            "%22last_name%22%3A%22Kibenko%22%2C%22username%22%3A%22vdkfrost%22%2C%22language_code%22%3A%22ru%22%2C"
            "%22is_premium%22%3Atrue%2C%22allows_write_to_pm%22%3Atrue%2C%22photo_url%22%3A%22https%3A%5C%2F%5C%2F"
            "t.me%5C%2Fi%5C%2Fuserpic%5C%2F320%5C%2F4FPEE4tmP3ATHa57u6MqTDih13LTOiMoKoLDRG4PnSA.svg%22%7D"
            "&chat_instance=8134722200314281151&chat_type=private&auth_date=1733509682"
            "&signature=TYJxVcisqbWjtodPepiJ6ghziUL94-KNpG8Pau-X7oNNLNBM72APCpi_RKiUlBvcqo5L-LAxIc3dnTzcZX_PDg"
            "&hash=a433d8f9847bd6addcc563bff7cc82c89e97ea0d90c11fe5729cae6796a36d73"
        )
        token = "7342037359:AAHI25ES9xCOMPokpYoz-p8XVrZUdygo2J4"
        self.assertEqual(
            verify_init_data(raw, token, {279058397}, now=1733509682),
            279058397,
        )
        with self.assertRaises(InvalidInitData):
            verify_init_data(raw.replace("signature=", "altered="), token, {279058397}, now=1733509682)

    def test_accepts_without_signature_and_rejects_stale_or_future_data(self) -> None:
        raw = signed_data(self.fields)
        self.assertEqual(verify_init_data(raw, TOKEN, {101}, now=1000), 101)
        with self.assertRaises(InvalidInitData):
            verify_init_data(raw, TOKEN, {101}, now=4601)
        with self.assertRaises(InvalidInitData):
            verify_init_data(raw, TOKEN, {101}, now=939)

    def test_rejects_duplicates_and_malformed_data(self) -> None:
        for key in ("hash", "auth_date", "user", "signature", "unknown"):
            with self.subTest(key=key):
                raw = signed_data([*self.fields, ("signature", "x"), ("unknown", "y")])
                with self.assertRaises(InvalidInitData):
                    verify_init_data(raw + f"&{key}=duplicate", TOKEN, {101}, now=1000)
        with self.assertRaises(InvalidInitData):
            verify_init_data("user=%ZZ&hash=abc", TOKEN, {101}, now=1000)

    def test_does_not_parse_user_until_after_hmac(self) -> None:
        raw = signed_data([("auth_date", "1000"), ("user", "invalid-json")])
        with self.assertRaisesRegex(InvalidInitData, "signature"):
            verify_init_data(raw.replace("invalid-json", "changed"), TOKEN, {101}, now=1000)
        with self.assertRaisesRegex(InvalidInitData, "Missing Telegram user"):
            verify_init_data(raw, TOKEN, {101}, now=1000)

    def test_rejects_user_outside_allowlist_or_boolean_id(self) -> None:
        with self.assertRaises(InvalidInitData):
            verify_init_data(signed_data(self.fields), TOKEN, {202}, now=1000)
        raw = signed_data([("auth_date", "1000"), ("user", '{"id":true}')])
        with self.assertRaises(InvalidInitData):
            verify_init_data(raw, TOKEN, {1}, now=1000)


class GroupingTests(unittest.TestCase):
    def test_date_groups_use_local_day_and_stable_id_order(self) -> None:
        records = [
            TranscriptSummary(1, "2026-09-23T00:30:00+00:00", "One", ()),
            TranscriptSummary(2, "2026-09-23T00:30:00+00:00", "Two", ()),
            TranscriptSummary(3, "2026-09-23T20:00:00+00:00", "Three", ()),
        ]
        groups = group_by_date(records, "America/Costa_Rica")
        self.assertEqual([group["key"] for group in groups], ["2026-09-23", "2026-09-22"])
        self.assertEqual([item["id"] for item in groups[1]["items"]], [2, 1])
        self.assertEqual(len(group_by_date(records, "UTC")), 1)
        with self.assertRaises(ValueError):
            group_by_date(records, "not/a-zone")

    def test_greedy_tags_recount_and_hide_only_grouping_tag(self) -> None:
        a, b, c = TagRecord(1, "a"), TagRecord(2, "b"), TagRecord(3, "c")
        records = [
            TranscriptSummary(1, "2026-09-24T10:00:00+00:00", "One", (a, b)),
            TranscriptSummary(2, "2026-09-23T10:00:00+00:00", "Two", (a, b)),
            TranscriptSummary(3, "2026-09-22T10:00:00+00:00", "Three", (a, c)),
            TranscriptSummary(4, "2026-09-21T10:00:00+00:00", "Four", (b,)),
            TranscriptSummary(5, "2026-09-20T10:00:00+00:00", "Five", (c,)),
            TranscriptSummary(6, "2026-09-19T10:00:00+00:00", "Six", ()),
        ]
        groups = group_by_tags(records)
        self.assertEqual([group["tag"] for group in groups], ["a", "b", "c", None])
        self.assertEqual([item["id"] for item in groups[0]["items"]], [1, 2, 3])
        self.assertEqual(groups[0]["items"][0]["tags"], ["b"])
        self.assertEqual([item["id"] for group in groups for item in group["items"]], [1, 2, 3, 4, 5, 6])
        self.assertTrue(groups[-1]["untagged"])

    def test_tie_breaks_by_freshness_then_name(self) -> None:
        records = [
            TranscriptSummary(1, "2026-09-23T10:00:00+00:00", "One", (TagRecord(2, "z"),)),
            TranscriptSummary(2, "2026-09-24T10:00:00+00:00", "Two", (TagRecord(3, "c"),)),
            TranscriptSummary(3, "2026-09-24T10:00:00+00:00", "Three", (TagRecord(1, "b"),)),
        ]
        self.assertEqual([group["tag"] for group in group_by_tags(records)], ["b", "c", "z"])


class FakeApplication:
    def __init__(self):
        self.events = []
        self.bot = Mock()
        self.bot.set_webhook = AsyncMock(side_effect=self._webhook)
        self.update_queue = asyncio.Queue()

    async def _webhook(self, **kwargs):
        self.events.append("set_webhook")

    async def initialize(self):
        self.events.append("initialize")

    async def start(self):
        self.events.append("start")

    async def stop(self):
        self.events.append("stop")

    async def shutdown(self):
        self.events.append("shutdown")


class MiniAppWebTests(unittest.TestCase):
    def test_webhook_registration_failure_stops_application(self) -> None:
        application = FakeApplication()

        async def fail_webhook(**kwargs):
            application.events.append("set_webhook")
            raise RuntimeError("registration failed")

        application.bot.set_webhook = AsyncMock(side_effect=fail_webhook)
        web = create_web_application(
            application,
            webhook_path="/hooks/bot/telegram/webhook",
            webhook_url="https://example.test/hooks/bot/telegram/webhook",
            webhook_secret_token="webhook-secret",
            app_path="/apps/bot/",
            bot_token=TOKEN,
            allowed_user_ids={101},
            transcript_store=Mock(),
            configure_menu_buttons=AsyncMock(),
        )
        with self.assertRaisesRegex(RuntimeError, "registration failed"):
            with TestClient(web):
                pass
        self.assertEqual(
            application.events,
            ["initialize", "start", "set_webhook", "stop", "shutdown"],
        )

    def test_http_routes_auth_and_lifecycle(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptStore(Path(directory) / "database.sqlite3")
            store.initialize()
            store.save(
                TranscriptRecord(101, 101, 1, "2026-09-24T10:00:00+00:00", "My title",
                                 "Private text", "voices/my.ogg", "transcripts/my")
            )
            store.save(
                TranscriptRecord(202, 202, 2, "2026-09-24T11:00:00+00:00", "Other title",
                                 "Other text", "voices/other.ogg", "transcripts/other")
            )
            other_tag = store.create_tag(202, "private", "Another user's tag")
            with sqlite3.connect(store.database_path) as connection:
                own_id = connection.execute("SELECT id FROM transcripts WHERE telegram_user_id=101").fetchone()[0]
                other_id = connection.execute("SELECT id FROM transcripts WHERE telegram_user_id=202").fetchone()[0]
            application = FakeApplication()
            configure_menu = AsyncMock()
            web = create_web_application(
                application,
                webhook_path="/hooks/bot/telegram/webhook",
                webhook_url="https://example.test/hooks/bot/telegram/webhook",
                webhook_secret_token="webhook-secret",
                app_path="/apps/bot/",
                bot_token=TOKEN,
                allowed_user_ids={101},
                transcript_store=store,
                configure_menu_buttons=configure_menu,
            )
            init_data = signed_data([("auth_date", str(int(time.time()))), ("user", '{"id":101}')])
            headers = {"X-Telegram-Init-Data": init_data}
            with TestClient(web) as client:
                page = client.get("/apps/bot/")
                self.assertEqual(page.status_code, 200)
                for label in ("Memos", "By Date", "By Categories", "← Back"):
                    self.assertIn(label, page.text)
                self.assertNotIn("Ваши заметки", page.text)
                script = client.get("/apps/bot/app.js")
                self.assertEqual(script.status_code, 200)
                self.assertIn("Uncategorized", script.text)
                self.assertEqual(client.get("/apps/bot/styles.css").status_code, 200)
                self.assertEqual(client.get("/apps/bot/api/groups?view=date").status_code, 401)
                response = client.get("/apps/bot/api/groups?view=date&timezone=UTC", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual([item["id"] for item in response.json()["groups"][0]["items"]], [own_id])
                self.assertEqual(client.get("/apps/bot/api/groups?view=bad", headers=headers).status_code, 400)
                self.assertEqual(client.get("/apps/bot/api/groups?view=date&timezone=bad", headers=headers).status_code, 400)
                self.assertEqual(client.get("/apps/bot/api/groups?view=tags&timezone=bad", headers=headers).status_code, 400)
                detail = client.get(f"/apps/bot/api/transcripts/{own_id}", headers=headers)
                self.assertEqual(detail.json()["text"], "Private text")
                self.assertEqual(client.get(f"/apps/bot/api/transcripts/{other_id}", headers=headers).status_code, 404)
                self.assertEqual(client.get("/apps/bot/api/tags").status_code, 401)
                self.assertEqual(client.post("/apps/bot/api/tags", json={"name": "work", "description": "Work"}).status_code, 401)
                self.assertEqual(client.get("/apps/bot/api/tags", headers=headers).json(), {"tags": []})
                created = client.post("/apps/bot/api/tags", json={"name": " #Work ", "description": " Work notes "}, headers=headers)
                self.assertEqual(created.status_code, 201)
                tag_id = created.json()["id"]
                self.assertEqual(created.json()["name"], "work")
                self.assertEqual(created.json()["description"], "Work notes")
                self.assertEqual(client.get("/apps/bot/api/tags", headers=headers).json()["tags"], [created.json()])
                self.assertEqual(client.post("/apps/bot/api/tags", json={"name": "WORK", "description": "Duplicate"}, headers=headers).status_code, 409)
                self.assertEqual(client.post("/apps/bot/api/tags", json={"name": " ", "description": "Invalid"}, headers=headers).status_code, 400)
                self.assertEqual(client.post("/apps/bot/api/tags", json={"name": "valid"}, headers=headers).status_code, 400)
                self.assertEqual(client.put(f"/apps/bot/api/tags/{other_tag.id}", json={"name": "stolen", "description": "No"}, headers=headers).status_code, 404)
                self.assertEqual(client.delete(f"/apps/bot/api/tags/{other_tag.id}", headers=headers).status_code, 404)
                store.save_with_tags(TranscriptRecord(101, 101, 1, "2026-09-24T10:00:00+00:00", "My title", "Private text", "voices/my.ogg", "transcripts/my"), (tag_id,))
                updated = client.put(f"/apps/bot/api/tags/{tag_id}", json={"name": "project", "description": "Project notes"}, headers=headers)
                self.assertEqual(updated.status_code, 200)
                self.assertEqual(updated.json()["name"], "project")
                self.assertEqual(updated.json()["description"], "Project notes")
                date_groups = client.get("/apps/bot/api/groups?view=date&timezone=UTC", headers=headers).json()["groups"]
                self.assertEqual(date_groups[0]["items"][0]["tags"], ["project"])
                tag_groups = client.get("/apps/bot/api/groups?view=tags&timezone=UTC", headers=headers).json()["groups"]
                self.assertEqual(tag_groups[0]["tag"], "project")
                self.assertEqual(client.delete(f"/apps/bot/api/tags/{tag_id}", headers=headers).status_code, 204)
                self.assertEqual(client.get(f"/apps/bot/api/transcripts/{own_id}", headers=headers).status_code, 200)
                self.assertEqual(client.get("/apps/bot/api/groups?view=date&timezone=UTC", headers=headers).json()["groups"][0]["items"][0]["tags"], [])
                self.assertEqual(client.get("/apps/bot/api/tags", headers=headers).json(), {"tags": []})
                self.assertEqual(store.list_tags_for_user(202), (other_tag,))
                self.assertEqual(client.post("/hooks/bot/telegram/webhook", json={"update_id": 1}).status_code, 403)
                with patch("vorec.miniapp_web.Update.de_json", return_value=object()):
                    self.assertEqual(client.post("/hooks/bot/telegram/webhook", json={"update_id": 1},
                                                 headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"}).status_code, 200)
                self.assertFalse(application.update_queue.empty())
            configure_menu.assert_awaited_once()
            self.assertEqual(application.events, ["initialize", "start", "set_webhook", "stop", "shutdown"])


if __name__ == "__main__":
    unittest.main()
