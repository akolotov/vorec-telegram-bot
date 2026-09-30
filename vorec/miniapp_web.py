"""HTTP routes shared by the Telegram webhook and transcript Mini App."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from telegram import Update

from vorec.library import group_by_date, transcript_item, transcript_detail, validated_timezone
from vorec.miniapp_auth import InvalidInitData, verify_init_data
from vorec.storage import (
    TagConflictError,
    TagValidationError,
    TitleValidationError,
    TranscriptStorageError,
    TranscriptStore,
)


LOGGER = logging.getLogger(__name__)
FRONTEND_DIRECTORY = Path(__file__).resolve().parent.parent / "miniapp"
NO_STORE = {"Cache-Control": "no-store"}
TITLE_JOB_TTL_SECONDS = 600
TITLE_JOB_LIMIT = 32


@dataclass
class TitleJob:
    user_id: int
    transcript_id: int
    created_at: float
    status: str = "pending"
    title: str | None = None
    finished_at: float | None = None
    task: asyncio.Task[None] | None = None


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
    suggest_title: Callable[[str, str], Awaitable[str]],
) -> Starlette:
    """Serve bot updates, a static Mini App, and its authenticated API."""

    title_jobs: dict[str, TitleJob] = {}

    def prune_title_jobs() -> None:
        now = time.monotonic()
        for job_id, job in tuple(title_jobs.items()):
            if job.finished_at is not None and now - job.finished_at >= TITLE_JOB_TTL_SECONDS:
                del title_jobs[job_id]

    async def run_title_job(job_id: str, text: str) -> None:
        job = title_jobs[job_id]
        try:
            title = await suggest_title(text, job_id)
            if not isinstance(title, str) or not title.strip():
                raise ValueError("Empty title suggestion")
            job.title = title.strip()
            job.status = "ready"
            LOGGER.info("Title job %s completed in %.1fs.", job_id, time.monotonic() - job.created_at)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            job.status = "failed"
            LOGGER.warning(
                "Title job %s failed after %.1fs (%s).",
                job_id, time.monotonic() - job.created_at, error.__class__.__name__,
            )
        finally:
            job.finished_at = time.monotonic()

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
            grouped = (
                group_by_date(transcript_store.list_for_user(user_id), timezone_name)
                if view == "date"
                else transcript_store.category_counts_for_user(user_id)
            )
        except ValueError:
            return JSONResponse({"error": "Invalid timezone"}, status_code=400, headers=NO_STORE)
        except TranscriptStorageError:
            LOGGER.exception("Could not load transcript list.")
            return JSONResponse({"error": "Unavailable"}, status_code=500, headers=NO_STORE)
        return JSONResponse({"view": view, "groups": grouped}, headers=NO_STORE)

    async def category_items(request: Request) -> Response:
        user_id = authenticated_user(request)
        if user_id is None:
            return JSONResponse({"error": "Unauthorized"}, status_code=401, headers=NO_STORE)
        params = request.query_params
        tag_id = None
        if len(params.multi_items()) != 1:
            return JSONResponse({"error": "Expected one category filter"}, status_code=400, headers=NO_STORE)
        if "tag_id" in params:
            raw_id = params["tag_id"]
            if not raw_id.isascii() or not raw_id.isdecimal() or len(raw_id) > 19:
                return JSONResponse({"error": "Invalid tag ID"}, status_code=400, headers=NO_STORE)
            tag_id = int(raw_id)
            if not 0 < tag_id <= 9223372036854775807:
                return JSONResponse({"error": "Invalid tag ID"}, status_code=400, headers=NO_STORE)
        elif params.get("untagged") != "1":
            return JSONResponse({"error": "Invalid category filter"}, status_code=400, headers=NO_STORE)
        try:
            if tag_id is not None and not any(
                tag.id == tag_id for tag in transcript_store.list_tags_for_user(user_id)
            ):
                return JSONResponse({"error": "Not found"}, status_code=404, headers=NO_STORE)
            records = transcript_store.list_for_user(user_id, tag_id=tag_id, untagged=tag_id is None)
        except TranscriptStorageError:
            LOGGER.exception("Could not load category items.")
            return JSONResponse({"error": "Unavailable"}, status_code=500, headers=NO_STORE)
        return JSONResponse({"items": [transcript_item(record) for record in records]}, headers=NO_STORE)

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

    async def generate_title(request: Request) -> Response:
        user_id = authenticated_user(request)
        if user_id is None:
            return JSONResponse({"error": "Unauthorized"}, status_code=401, headers=NO_STORE)
        transcript_id = request.path_params["transcript_id"]
        try:
            record = transcript_store.get_for_user(transcript_id, user_id)
        except TranscriptStorageError:
            LOGGER.exception("Could not load transcript for title generation.")
            return JSONResponse({"error": "Unavailable"}, status_code=500, headers=NO_STORE)
        if record is None:
            return JSONResponse({"error": "Not found"}, status_code=404, headers=NO_STORE)
        prune_title_jobs()
        for job_id, job in title_jobs.items():
            if job.user_id == user_id and job.transcript_id == transcript_id and job.status == "pending":
                return JSONResponse({"task_id": job_id}, status_code=202, headers=NO_STORE)
        if len(title_jobs) >= TITLE_JOB_LIMIT:
            finished_id = next(
                (job_id for job_id, job in title_jobs.items() if job.finished_at is not None),
                None,
            )
            if finished_id is None:
                return JSONResponse({"error": "Too many title jobs"}, status_code=429, headers=NO_STORE)
            del title_jobs[finished_id]
        job_id = secrets.token_urlsafe(12)
        job = TitleJob(user_id, transcript_id, time.monotonic())
        title_jobs[job_id] = job
        job.task = asyncio.create_task(run_title_job(job_id, record.text))
        LOGGER.info("Title job %s created for transcript %d.", job_id, transcript_id)
        return JSONResponse({"task_id": job_id}, status_code=202, headers=NO_STORE)

    async def title_job_status(request: Request) -> Response:
        user_id = authenticated_user(request)
        if user_id is None:
            return JSONResponse({"error": "Unauthorized"}, status_code=401, headers=NO_STORE)
        prune_title_jobs()
        job = title_jobs.get(request.path_params["job_id"])
        if job is None or job.user_id != user_id:
            return JSONResponse({"error": "Not found"}, status_code=404, headers=NO_STORE)
        if job.status == "ready":
            return JSONResponse({"status": "ready", "title": job.title}, headers=NO_STORE)
        return JSONResponse({"status": job.status}, headers=NO_STORE)

    async def update_title(request: Request) -> Response:
        user_id = authenticated_user(request)
        if user_id is None:
            return JSONResponse({"error": "Unauthorized"}, status_code=401, headers=NO_STORE)
        if request.headers.get("content-type", "").partition(";")[0] != "application/json":
            return JSONResponse({"error": "Expected JSON"}, status_code=415, headers=NO_STORE)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400, headers=NO_STORE)
        if not isinstance(body, dict) or not isinstance(body.get("title"), str):
            return JSONResponse({"error": "A title is required"}, status_code=400, headers=NO_STORE)
        transcript_id = request.path_params["transcript_id"]
        try:
            updated = transcript_store.update_title_for_user(
                transcript_id, user_id, body["title"]
            )
            if not updated:
                return JSONResponse({"error": "Not found"}, status_code=404, headers=NO_STORE)
            record = transcript_store.get_for_user(transcript_id, user_id)
        except TitleValidationError as error:
            return JSONResponse({"error": str(error)}, status_code=400, headers=NO_STORE)
        except TranscriptStorageError:
            LOGGER.exception("Could not update transcript title.")
            return JSONResponse({"error": "Unavailable"}, status_code=500, headers=NO_STORE)
        if record is None:
            return JSONResponse({"error": "Not found"}, status_code=404, headers=NO_STORE)
        return JSONResponse(transcript_detail(record), headers=NO_STORE)

    async def update_transcript_tags(request: Request) -> Response:
        user_id = authenticated_user(request)
        if user_id is None:
            return JSONResponse({"error": "Unauthorized"}, status_code=401, headers=NO_STORE)
        if request.headers.get("content-type", "").partition(";")[0] != "application/json":
            return JSONResponse({"error": "Expected JSON"}, status_code=415, headers=NO_STORE)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400, headers=NO_STORE)
        tag_ids = body.get("tag_ids") if isinstance(body, dict) else None
        if (
            not isinstance(tag_ids, list)
            or any(type(tag_id) is not int or tag_id <= 0 for tag_id in tag_ids)
            or len(tag_ids) != len(set(tag_ids))
        ):
            return JSONResponse({"error": "Invalid tag IDs"}, status_code=400, headers=NO_STORE)
        try:
            transcript_id = int(request.path_params["transcript_id"])
            updated = transcript_store.replace_tags_for_user(
                transcript_id, user_id, tuple(tag_ids)
            )
            if not updated:
                return JSONResponse({"error": "Not found"}, status_code=404, headers=NO_STORE)
            record = transcript_store.get_for_user(transcript_id, user_id)
        except TagValidationError as error:
            return JSONResponse({"error": str(error)}, status_code=400, headers=NO_STORE)
        except TranscriptStorageError:
            LOGGER.exception("Could not update transcript tags.")
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
                owned_tags = transcript_store.list_tag_usage_for_user(user_id)
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
                for job in title_jobs.values():
                    if job.task is not None and not job.task.done():
                        job.task.cancel()
                await asyncio.gather(
                    *(job.task for job in title_jobs.values() if job.task is not None),
                    return_exceptions=True,
                )
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
            Route(app_path + "api/transcripts", category_items, methods=["GET"]),
            Route(app_path + "api/transcripts/{transcript_id:int}", detail, methods=["GET"]),
            Route(
                app_path + "api/transcripts/{transcript_id:int}/generate-title",
                generate_title,
                methods=["POST"],
            ),
            Route(app_path + "api/title-jobs/{job_id}", title_job_status, methods=["GET"]),
            Route(
                app_path + "api/transcripts/{transcript_id:int}/title",
                update_title,
                methods=["PUT"],
            ),
            Route(
                app_path + "api/transcripts/{transcript_id:int}/tags",
                update_transcript_tags,
                methods=["PUT"],
            ),
            Route(app_path + "api/tags", tags, methods=["GET", "POST"]),
            Route(app_path + "api/tags/{tag_id:int}", tag, methods=["PUT", "DELETE"]),
        ],
        lifespan=lifespan,
    )
