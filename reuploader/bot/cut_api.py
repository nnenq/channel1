"""API мини-апки для умной обрезки: загрузка кусками, запуск, статус, скачивание."""
import asyncio
import json
import re
from pathlib import Path

from aiohttp import web

from ..smartcut.core import format_report
from ..smartcut.media import probe
from ..smartcut.target import fit_params, parse_list, parse_range, target_from_channel, target_from_videos
from .cutjobs import job_dir

CHUNK = 8 * 1024 * 1024
SAFE_NAME = re.compile(r"[^\w.\- ()\[\]а-яА-ЯёЁ]+")


def setup(webapp, router):
    api = CutApi(webapp)
    router.add_get("/api/cut", api.list)
    router.add_post("/api/cut", api.create)
    router.add_get("/api/cut/{jid}", api.get)
    router.add_put("/api/cut/{jid}/chunk", api.chunk)
    router.add_post("/api/cut/{jid}/run", api.run)
    router.add_delete("/api/cut/{jid}", api.delete)
    router.add_post("/api/cut/{jid}/ai", api.decide_ai)
    router.add_get("/api/balance", api.balance)
    router.add_post("/api/balance/topup", api.add_topup)
    router.add_delete("/api/balance/topup/{tid}", api.delete_topup)
    router.add_get("/dl/{token}", api.download)
    return api


class CutApi:
    def __init__(self, webapp):
        self.w = webapp
        self.db = webapp.db
        self.s = webapp.s

    def _job(self, request):
        from .web import ApiError

        job = self.db.cut_job(int(request.match_info["jid"]))
        if not job or job["user_id"] != request["user"]["id"]:
            raise ApiError("Задача не найдена.", status=404)
        return job

    def _json(self, job):
        report = json.loads(job["report"]) if job["report"] else None
        uploaded = 0
        if job["status"] == "uploading" and job["src_path"] and Path(job["src_path"]).exists():
            uploaded = Path(job["src_path"]).stat().st_size
        return {
            "id": job["id"], "status": job["status"], "stage": job["stage"], "progress": job["progress"],
            "filename": job["filename"], "size": job["size"], "uploaded": uploaded,
            "target": job["target"], "target_info": job["target_info"], "error": job["error"],
            "report": report, "report_text": format_report(report) if report else None,
            "link": (f"/dl/{job['dl_token']}" if self.w.bot.cut.link_valid(job) else None),
            "mode": job["mode"], "ai_state": job["ai_state"],
            "estimate": json.loads(job["estimate"]) if job["estimate"] else None,
        }

    async def list(self, request):
        jobs = self.db.cut_jobs(request["user"]["id"])
        return web.json_response({"jobs": [self._json(j) for j in jobs], "chunk": CHUNK,
                                  "max_mb": self.s.cut_max_mb, "max_minutes": self.s.cut_max_minutes,
                                  "ai_available": self.w.bot.cut.ai_allowed(request["user"]["id"])})

    async def get(self, request):
        return web.json_response(self._json(self._job(request)))

    async def create(self, request):
        from .web import ApiError

        body = await request.json()
        size = int(body.get("size") or 0)
        name = SAFE_NAME.sub("_", str(body.get("filename") or "video.mp4"))[:120]
        if size <= 0:
            raise ApiError("Пустой файл.")
        if size > self.s.cut_max_mb * 1024 * 1024:
            raise ApiError(f"Файл больше {self.s.cut_max_mb} МБ.")
        jid = self.db.create_cut_job(request["user"]["id"], name, size)
        d = job_dir(self.s, jid)
        d.mkdir(parents=True, exist_ok=True)
        src = d / ("src" + (Path(name).suffix.lower() or ".mp4"))
        src.touch()
        self.db.update_cut_job(jid, src_path=str(src))
        return web.json_response(self._json(self.db.cut_job(jid)) | {"chunk": CHUNK})

    async def chunk(self, request):
        """PUT сырых байтов по смещению ?offset=N. Можно продолжать после обрыва."""
        from .web import ApiError

        job = self._job(request)
        if job["status"] != "uploading":
            raise ApiError("Загрузка уже завершена.")
        src = Path(job["src_path"])
        offset = int(request.query.get("offset", "0"))
        have = src.stat().st_size
        if offset != have:
            return web.json_response({"uploaded": have}, status=409)   # клиент продолжит с have
        written = 0
        with open(src, "ab") as f:
            async for part in request.content.iter_chunked(256 * 1024):
                written += len(part)
                if written > CHUNK + 1024 or have + written > job["size"]:
                    raise ApiError("Слишком большой кусок.")
                f.write(part)
        have += written
        if have >= job["size"]:
            try:
                info = await asyncio.get_running_loop().run_in_executor(None, probe, src)
            except Exception:  # noqa: BLE001
                self.db.update_cut_job(job["id"], status="failed", error="это не видео или файл повреждён")
                raise ApiError("Это не видео или файл повреждён.") from None
            if info.duration > self.s.cut_max_minutes * 60:
                self.db.update_cut_job(job["id"], status="failed", error="слишком длинное видео")
                raise ApiError(f"Видео длиннее {self.s.cut_max_minutes} минут.")
            self.db.update_cut_job(job["id"], status="uploaded", stage=f"длина {info.duration:.0f} с")
        return web.json_response(self._json(self.db.cut_job(job["id"])))

    async def run(self, request):
        """Запуск: {"mode": "manual", "seconds": 58} | {"mode": "channel", "url": ...} | {"mode": "list", "text": ...}"""
        from .web import ApiError

        job = self._job(request)
        if job["status"] not in ("uploaded", "failed", "done", "confirm") or not job["src_path"] \
                or not Path(job["src_path"]).exists():
            raise ApiError("Сначала дождись окончания загрузки." if job["status"] == "uploading"
                           else "Исходник уже удалён — загрузи видео заново.")
        body = await request.json()
        mode = body.get("mode")
        tolerance = 0.05
        if mode == "manual":
            try:
                lo, hi = parse_range(str(body.get("seconds", "")))
            except ValueError as e:
                raise ApiError(str(e)) from None
            if hi > lo:     # «плавающая» длина: итог где-то между lo и hi
                target, tolerance = fit_params(lo, hi)
                info = f"диапазон {int(lo // 60)}:{int(lo % 60):02d}–{int(hi // 60)}:{int(hi % 60):02d}"
            else:
                target, info = lo, "вручную"
        elif mode == "channel":
            url = str(body.get("url", "")).strip()
            if not url:
                raise ApiError("Вставь ссылку на канал.")
            try:
                target, n = await asyncio.get_running_loop().run_in_executor(None, target_from_channel, url)
            except Exception as e:  # noqa: BLE001
                raise ApiError(f"Не получилось прочитать канал: {e}") from None
            if not target:
                raise ApiError("Не нашёл длительности роликов канала — введи длину вручную.")
            info = f"как на канале: медиана {n} лучших роликов"
        elif mode == "list":
            try:
                target, n = target_from_videos(parse_list(str(body.get("text", ""))))
            except ValueError as e:
                raise ApiError(str(e)) from None
            if not target:
                raise ApiError("Нужны строки вида «0:45 120k».")
            info = f"по списку: медиана {n} лучших"
        else:
            raise ApiError("Выбери, как задать длину.")
        if not 3 <= target <= 3 * 3600:
            raise ApiError("Странная целевая длина.")
        ai = bool(body.get("ai")) and self.w.bot.cut.ai_allowed(request["user"]["id"])
        self.db.update_cut_job(job["id"], status="queued", stage="в очереди", progress=0, target=target,
                               target_info=info, error=None, report=None, tolerance=tolerance,
                               mode="ai" if ai else "free", ai_state=None, estimate=None)
        self.w.bot.cut.poke()
        return web.json_response(self._json(self.db.cut_job(job["id"])))

    async def decide_ai(self, request):
        from .web import ApiError

        job = self._job(request)
        ok, text = self.w.bot.cut.decide_ai(job, bool((await request.json()).get("yes")))
        if not ok:
            raise ApiError(text)
        return web.json_response(self._json(self.db.cut_job(job["id"])))

    # --- баланс Claude API (только владелец: см. проверку в web.auth_mw) ---
    async def balance(self, request):
        s = self.w.bot.cut.balance.summary()
        return web.json_response(s | {"ai_enabled": self.w.bot.cut.ai_allowed(request["user"]["id"])})

    async def add_topup(self, request):
        from datetime import date

        from .web import ApiError

        body = await request.json()
        try:
            amount = round(float(str(body.get("amount", "")).replace(",", ".").replace("$", "")), 2)
            day = date.fromisoformat(str(body.get("date") or date.today().isoformat())).isoformat()
        except ValueError:
            raise ApiError("Сумма числом, дата в формате ГГГГ-ММ-ДД.") from None
        if not 0 < amount < 100000:
            raise ApiError("Странная сумма.")
        self.db.add_topup(amount, day, (body.get("note") or "")[:100] or None)
        await self.w.bot.cut.balance.refresh_admin(force=True)
        return await self.balance(request)

    async def delete_topup(self, request):
        self.db.delete_topup(int(request.match_info["tid"]))
        return await self.balance(request)

    async def delete(self, request):
        import shutil

        from .web import ApiError

        job = self._job(request)
        if job["status"] in ("running", "queued"):
            raise ApiError("Задача уже в работе.")
        shutil.rmtree(job_dir(self.s, job["id"]), ignore_errors=True)
        self.db.x("DELETE FROM cut_jobs WHERE id = ?", job["id"])
        return web.json_response({"ok": True})

    async def download(self, request):
        job = self.db.cut_job_by_token(request.match_info["token"])
        if not self.w.bot.cut.link_valid(job):
            raise web.HTTPNotFound(text="Ссылка устарела или файл уже удалён.")
        name = Path(job["filename"]).stem + "_short.mp4"
        return web.FileResponse(job["out_path"], headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{_quote(name)}",
            "Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


def _quote(s):
    from urllib.parse import quote

    return quote(s)

