"""Authenticated, project-scoped thumbnail drafts and immutable publication copies."""
import asyncio
import base64
import io
import logging
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

from aiohttp import web
from PIL import Image

from .. import covers
from ..source import download
from .db import iso, utcnow

log = logging.getLogger(__name__)


def setup(owner, app):
    api = CoverApi(owner)
    root = "/api/projects/{pid}/covers"
    app.router.add_post(root, api.create)
    app.router.add_get(root + "/{cid}", api.get)
    app.router.add_post(root + "/{cid}/preview", api.preview)
    app.router.add_post(root + "/{cid}/image", api.image)
    app.on_cleanup.append(api.shutdown)
    return api


class CoverApi:
    def __init__(self, owner):
        self.w, self.db = owner, owner.db
        self.root = owner.s.data_dir / "covers"
        self.tasks = set()
        self.busy = asyncio.Semaphore(1)
        self.db.x("UPDATE cover_drafts SET status='failed', error='Бот перезапущен — создай варианты заново.' "
                  "WHERE status IN ('queued', 'running')")

    def directory(self, cid):
        return self.root / cid

    def remove_directory(self, directory):
        target = Path(directory).resolve()
        root = self.root.resolve()
        if target == root or not target.is_relative_to(root):
            raise ValueError("Invalid cover directory")
        shutil.rmtree(target, ignore_errors=True)

    def draft(self, request):
        from .web import ApiError
        project = self.w._project(request)
        row = self.db.one("SELECT * FROM cover_drafts WHERE id=? AND project_id=?",
                          request.match_info["cid"], project["id"])
        if not row:
            raise ApiError("Обложка не найдена.", 404)
        return row

    async def shutdown(self, app):
        # Let executor work finish before the server/database is closed.
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    def cleanup(self):
        for row in self.db.q("SELECT id FROM cover_drafts WHERE created_at<? AND status NOT IN ('queued','running')",
                             iso(utcnow() - timedelta(days=1))):
            self.remove_directory(self.directory(row["id"]))
            self.db.x("DELETE FROM cover_drafts WHERE id=?", row["id"])

    async def create(self, request):
        from .web import ApiError, normalize_video
        p = self.w._project(request)
        body = await request.json()
        url = normalize_video(body.get("video_url", ""))
        if not url:
            raise ApiError("Нужна ссылка на видео YouTube или TikTok.")
        self.cleanup()
        if self.db.one("SELECT id FROM cover_drafts WHERE project_id=? AND status IN ('queued','running')", p["id"]):
            raise ApiError("Дождись подготовки предыдущих обложек.", 409)
        if len(self.tasks) >= 4:
            raise ApiError("Очередь обложек занята. Попробуй чуть позже.", 429)
        cid = uuid.uuid4().hex
        self.db.x("INSERT INTO cover_drafts(id,project_id,video_url,title,status,created_at) VALUES(?,?,?,?,?,?)",
                  cid, p["id"], url, str(body.get("title") or "")[:100], "queued", iso(utcnow()))
        if body.get("custom_only"):
            self.db.x("UPDATE cover_drafts SET status='custom' WHERE id=?", cid)
        else:
            task = asyncio.create_task(self.generate(cid, url))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return web.json_response({"id": cid, "status": "queued"}, status=202)

    async def generate(self, cid, url):
        async with self.busy:
            self.db.x("UPDATE cover_drafts SET status='running' WHERE id=?", cid)
            folder = self.directory(cid)
            def work():
                try:
                    video, meta = download(url, folder / "source", preview=True)
                    if not video.exists():
                        raise ValueError("Видео слишком большое или не удалось скачать.")
                    covers.extract_frames(video, folder)
                    return meta["title"]
                finally:
                    self.remove_directory(folder / "source")
            try:
                title = await asyncio.to_thread(work)
                self.db.x("UPDATE cover_drafts SET status='ready', title=CASE WHEN title='' THEN ? ELSE title END WHERE id=?",
                          title[:100], cid)
            except Exception:
                log.exception("thumbnail generation failed: %s", cid)
                self.db.x("UPDATE cover_drafts SET status='failed', error=? WHERE id=?",
                          "Не удалось получить кадры. Проверь доступность ролика или загрузи свою картинку.", cid)

    async def get(self, request):
        row = self.draft(request)
        return web.json_response({k: row[k] for k in ("id", "status", "error", "title")} |
                                 {"text": covers.suggested_text(row["title"]),
                                  "custom": (self.directory(row["id"]) / "custom.jpg").exists()})

    def options(self, body):
        from .web import ApiError
        style = body.get("style", "lemon")
        text = str(body.get("text") or "").strip()
        if style not in covers.STYLES or len(text) > 100:
            raise ApiError("Выбери стиль и надпись до 100 символов.")
        return text, style

    async def preview(self, request):
        from .web import ApiError
        row = self.draft(request)
        if row["status"] != "ready":
            raise ApiError("Кадры ещё не готовы.", 409)
        text, style = self.options(await request.json())
        def work():
            images = []
            for i in range(3):
                # Unique temp output prevents concurrent preview requests clobbering each other.
                out = self.directory(row["id"]) / (uuid.uuid4().hex + ".jpg")
                try:
                    covers.render(self.directory(row["id"]) / f"frame{i}.jpg", text, style, out)
                    images.append(self.small_image(out))
                finally:
                    out.unlink(missing_ok=True)
            return images
        return web.json_response({"images": await asyncio.to_thread(work)})

    @staticmethod
    def small_image(path):
        with Image.open(path) as im:
            im.thumbnail((270, 480))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()

    async def image(self, request):
        from .web import ApiError
        row = self.draft(request)
        raw = await request.read()
        try:
            path = await asyncio.to_thread(covers.normalize_image, raw, self.directory(row["id"]) / "custom.jpg")
        except (ValueError, OSError, Image.DecompressionBombError) as e:
            raise ApiError("Не удалось прочитать картинку. Нужен JPG, PNG или WebP до 8 МБ.") from e
        return web.json_response({"image": self.small_image(path)})

    async def selection(self, pid, url, body):
        """Called only after publish validation; never trust a client-provided file path."""
        from .web import ApiError
        choice = body.get("cover_choice", "project")
        if choice in ("off", "project"):
            return None, choice
        if choice != "selected":
            raise ApiError("Неизвестный режим обложки.")
        row = self.db.one("SELECT * FROM cover_drafts WHERE id=? AND project_id=? AND video_url=?",
                          str(body.get("cover_id") or ""), pid, url)
        if not row:
            raise ApiError("Обложка относится к другому ролику или устарела.")
        text, style = self.options(body)
        index = str(body.get("cover_index"))
        if index not in ("0", "1", "2", "custom"):
            raise ApiError("Выбери одну из обложек.")
        src = self.directory(row["id"]) / ("custom.jpg" if index == "custom" else f"frame{index}.jpg")
        if not src.exists() or (index != "custom" and row["status"] != "ready"):
            raise ApiError("Картинка ещё не готова. Создай обложку заново.")
        out = self.root / "selected" / (uuid.uuid4().hex + ".jpg")
        out.parent.mkdir(parents=True, exist_ok=True)
        if index == "custom":
            await asyncio.to_thread(shutil.copyfile, src, out)
        else:
            await asyncio.to_thread(covers.render, src, text, style, out)
        return str(out), choice
