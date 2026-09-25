"""HTTP routes shared by the Telegram webhook and transcript Mini App."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from telegram import Update

from vorec.library import group_by_date, group_by_tags, transcript_detail, validated_timezone
from vorec.miniapp_auth import InvalidInitData, verify_init_data
from vorec.storage import (
    TagConflictError,
    TagValidationError,
    TranscriptStorageError,
    TranscriptStore,
)


LOGGER = logging.getLogger(__name__)
FRONTEND_DIRECTORY = Path(__file__).resolve().parent.parent / "miniapp"
NO_STORE = {"Cache-Control": "no-store"}


def create_web_application(
    application,
    *,
    webhook_path: str,
    webhook_url: str,
    webhook_secret_token: str,
    app_path: str,
    bot_token: str,
    allowed_user_ids: set[int],
    transcript_store: TranscriptStore,
    configure_menu_buttons,
) -> Starlette:
    """Serve bot updates, a static Mini App, and authenticated read-only API."""

    async def telegram_webhook(request: Request) -> Response:
        if not secrets.compare_digest(
            request.headers.get("x-telegram-bot-api-secret-token", ""),
            webhook_secret_token,
        ):
            return Response(status_code=403)
        if request.headers.get("content-type", "").partition(";")[0] != "application/json":
            return Response(status_code=415)
        try:
            update = Update.de_json(await request.json(), application.bot)
        except Exception:
            LOGGER.warning("Invalid Telegram webhook update.")
            return Response(status_code=400)
        if update is not None:
            await application.update_queue.put(update)
        return Response(status_code=200)

    async def frontend(request: Request) -> Response:
        return FileResponse(FRONTEND_DIRECTORY / "index.html", headers=NO_STORE)

    async def script(request: Request) -> Response:
        return FileResponse(FRONTEND_DIRECTORY / "app.js", media_type="text/javascript", headers=NO_STORE)

    async def stylesheet(request: Request) -> Response:
        return FileResponse(FRONTEND_DIRECTORY / "styles.css", media_type="text/css", headers=NO_STORE)

    def authenticated_user(request: Request) -> int | None:
        try:
            return verify_init_data(
                request.headers.get("x-telegram-init-data", ""),
                bot_token,
                allowed_user_ids,
            )
        except InvalidInitData:
            return None

    async def groups(request: Request) -> Response:
        user_id = authenticated_user(request)
        if user_id is None:
            return JSONResponse({"error": "Unauthorized"}, status_code=401, headers=NO_STORE)
        view = request.query_params.get("view")
        timezone_name = request.query_params.get("timezone", "UTC")
        if view not in ("date", "tags"):
            return JSONResponse({"error": "Invalid view"}, status_code=400, headers=NO_STORE)
        try:
            validated_timezone(timezone_name)
            records = transcript_store.list_for_user(user_id)
            grouped = (
                group_by_date(records, timezone_name)
                if view == "date"
                else group_by_tags(records)
            )
        except ValueError:
            return JSONResponse({"error": "Invalid timezone"}, status_code=400, headers=NO_STORE)
        except TranscriptStorageError:
            LOGGER.exception("Could not load transcript list.")
            return JSONResponse({"error": "Unavailable"}, status_code=500, headers=NO_STORE)
        return JSONResponse({"view": view, "groups": grouped}, headers=NO_STORE)

    async def detail(request: Request) -> Response:
        user_id = authenticated_user(request)
        if user_id is None:
            return JSONResponse({"error": "Unauthorized"}, status_code=401, headers=NO_STORE)
        try:
            transcript_id = int(request.path_params["transcript_id"])
            record = transcript_store.get_for_user(transcript_id, user_id)
        except TranscriptStorageError:
            LOGGER.exception("Could not load transcript.")
            return JSONResponse({"error": "Unavailable"}, status_code=500, headers=NO_STORE)
        if record is None:
            return JSONResponse({"error": "Not found"}, status_code=404, headers=NO_STORE)
        return JSONResponse(transcript_detail(record), headers=NO_STORE)

    async def tags(request: Request) -> Response:
        user_id = authenticated_user(request)
        if user_id is None:
            return JSONResponse({"error": "Unauthorized"}, status_code=401, headers=NO_STORE)
        if request.method == "GET":
            try:
                owned_tags = transcript_store.list_tags_for_user(user_id)
            except TranscriptStorageError:
                LOGGER.exception("Could not load tags.")
                return JSONResponse({"error": "Unavailable"}, status_code=500, headers=NO_STORE)
            return JSONResponse({"tags": [vars(tag) for tag in owned_tags]}, headers=NO_STORE)
        values = await tag_values(request)
        if isinstance(values, Response):
            return values
        try:
            tag = transcript_store.create_tag(user_id, *values)
        except TagValidationError as error:
            return JSONResponse({"error": str(error)}, status_code=400, headers=NO_STORE)
        except TagConflictError as error:
            return JSONResponse({"error": str(error)}, status_code=409, headers=NO_STORE)
        except TranscriptStorageError:
            LOGGER.exception("Could not create tag.")
            return JSONResponse({"error": "Unavailable"}, status_code=500, headers=NO_STORE)
        return JSONResponse(vars(tag), status_code=201, headers=NO_STORE)

    async def tag_values(request: Request) -> tuple[str, str] | Response:
        if request.headers.get("content-type", "").partition(";")[0] != "application/json":
            return JSONResponse({"error": "Expected JSON"}, status_code=415, headers=NO_STORE)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400, headers=NO_STORE)
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("name"), str)
            or not isinstance(body.get("description"), str)
        ):
            return JSONResponse({"error": "Name and description are required"}, status_code=400, headers=NO_STORE)
        return body["name"], body["description"]

    async def tag(request: Request) -> Response:
        user_id = authenticated_user(request)
        if user_id is None:
            return JSONResponse({"error": "Unauthorized"}, status_code=401, headers=NO_STORE)
        tag_id = request.path_params["tag_id"]
        if request.method == "DELETE":
            try:
                deleted = transcript_store.delete_tag(user_id, tag_id)
            except TranscriptStorageError:
                LOGGER.exception("Could not delete tag.")
                return JSONResponse({"error": "Unavailable"}, status_code=500, headers=NO_STORE)
            if not deleted:
                return JSONResponse({"error": "Not found"}, status_code=404, headers=NO_STORE)
            return Response(status_code=204, headers=NO_STORE)
        values = await tag_values(request)
        if isinstance(values, Response):
            return values
        try:
            updated = transcript_store.update_tag(user_id, tag_id, *values)
        except TagValidationError as error:
            return JSONResponse({"error": str(error)}, status_code=400, headers=NO_STORE)
        except TagConflictError as error:
            return JSONResponse({"error": str(error)}, status_code=409, headers=NO_STORE)
        except TranscriptStorageError:
            LOGGER.exception("Could not update tag.")
            return JSONResponse({"error": "Unavailable"}, status_code=500, headers=NO_STORE)
        if updated is None:
            return JSONResponse({"error": "Not found"}, status_code=404, headers=NO_STORE)
        return JSONResponse(vars(updated), headers=NO_STORE)

    @asynccontextmanager
    async def lifespan(_: Starlette):
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
            await configure_menu_buttons(application.bot, allowed_user_ids)
            yield
        finally:
            try:
                if started:
                    await application.stop()
            finally:
                if initialized:
                    await application.shutdown()

    return Starlette(
        routes=[
            Route(webhook_path, telegram_webhook, methods=["POST"]),
            Route(app_path, frontend, methods=["GET"]),
            Route(app_path + "app.js", script, methods=["GET"]),
            Route(app_path + "styles.css", stylesheet, methods=["GET"]),
            Route(app_path + "api/groups", groups, methods=["GET"]),
            Route(app_path + "api/transcripts/{transcript_id:int}", detail, methods=["GET"]),
            Route(app_path + "api/tags", tags, methods=["GET", "POST"]),
            Route(app_path + "api/tags/{tag_id:int}", tag, methods=["PUT", "DELETE"]),
        ],
        lifespan=lifespan,
    )
