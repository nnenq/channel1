"""API мини-апки для умной обрезки: загрузка кусками, запуск, статус, скачивание."""
import asyncio
import json
import re
from pathlib import Path

from aiohttp import web

from ..smartcut.media import probe
from ..smartcut.target import fit_params, parse_list, parse_range, target_from_channel, target_from_videos
from .cutjobs import job_dir, music_dir, report_text

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
    router.add_post("/api/cut/{jid}/cancel", api.cancel)
    router.add_get("/api/balance", api.balance)
    router.add_post("/api/balance/topup", api.add_topup)
    router.add_delete("/api/balance/topup/{tid}", api.delete_topup)
    router.add_get("/dl/{token}", api.download)
    router.add_post("/api/cut/{jid}/share", api.share)
    router.add_get("/share/{token}/{name}", api.shared_file)
    router.add_get("/api/music", api.music_list)
    router.add_put("/api/music", api.music_upload)
    router.add_patch("/api/music", api.music_prefs)
    router.add_get("/api/music/{name}", api.music_file)
    router.add_delete("/api/music/{name}", api.music_delete)
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
            "report": report, "report_text": report_text(report) if report else None,
            "link": (f"/dl/{job['dl_token']}" if self.w.bot.cut.link_valid(job) else None),
            "mode": job["mode"], "ai_state": job["ai_state"],
            "options": json.loads(job["options"]) if job["options"] else None,
            "estimate": json.loads(job["estimate"]) if job["estimate"] else None,
        }

    async def list(self, request):
        from .. import combo as cb

        jobs = self.db.cut_jobs(request["user"]["id"])
        return web.json_response({"jobs": [self._json(j) for j in jobs], "chunk": CHUNK,
                                  "combo": combo_defaults(self.db, request["user"]["id"]), "frames": cb.FRAMES,
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
        if mode == "combo":      # всё сразу по выбору: длина, уникализация, кадр, субтитры (+ музыка)
            from .. import combo as cb

            try:
                opts = cb.clean(body.get("options") or {})
            except ValueError as e:
                raise ApiError(str(e)) from None
            start_combo(self.db, job, opts)
            self.w.bot.cut.poke()
            return web.json_response(self._json(self.db.cut_job(job["id"])))
        if mode == "subs":       # замена вшитых субтитров — длина не нужна; method: erase | strip | crop
            method = body.get("method") if body.get("method") in ("strip", "crop") else "erase"
            self.db.update_cut_job(job["id"], status="queued", stage="в очереди", progress=0, target=None,
                                   target_info="субтитры: " + {"erase": "стереть", "strip": "полоска",
                                                               "crop": "обрезка"}[method], error=None,
                                   report=None, mode="subs" if method == "erase" else "subs_" + method,
                                   ai_state=None, estimate=None)
            self.w.bot.cut.poke()
            return web.json_response(self._json(self.db.cut_job(job["id"])))
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

    # --- фоновая музыка (свои треки пользователя) ---
    def _music_json(self, uid):
        from .. import music as mu

        items = []
        for p in mu.tracks(music_dir(self.s, uid)):
            items.append({"name": p.name, "size": p.stat().st_size})
        return web.json_response(self.db.prefs(uid) | {"tracks": items, "levels": mu.LEVEL_RU,
                                                        "builtin": mu.builtin_choices(),
                                                        "max_tracks": mu.MAX_TRACKS, "max_mb": mu.MAX_MB})

    def _track(self, request):
        from .. import music as mu
        from .music_builtin_api import builtin_file
        from .web import ApiError

        name = Path(request.match_info["name"]).name
        if name.startswith(mu.BUILTIN):
            return builtin_file(self.s, name)
        path = music_dir(self.s, request["user"]["id"]) / name
        if path.suffix.lower() not in mu.AUDIO_EXT or not path.is_file():
            raise ApiError("Трек не найден.", status=404)
        return path

    async def music_list(self, request):
        return self._music_json(request["user"]["id"])

    async def music_upload(self, request):
        """PUT сырых байтов трека, ?name=файл.mp3."""
        from .. import music as mu
        from .web import ApiError

        uid = request["user"]["id"]
        name = SAFE_NAME.sub("_", Path(request.query.get("name") or "track.mp3").name)[:80]
        if Path(name).suffix.lower() not in mu.AUDIO_EXT:
            raise ApiError("Нужен аудиофайл: mp3, m4a, wav, ogg, opus, flac.")
        d = music_dir(self.s, uid)
        d.mkdir(parents=True, exist_ok=True)
        if len(mu.tracks(d)) >= mu.MAX_TRACKS:
            raise ApiError(f"Не больше {mu.MAX_TRACKS} треков — удали лишние.")
        dst, tmp = d / name, d / (name + ".part")
        size = 0
        try:
            with open(tmp, "wb") as f:
                async for part in request.content.iter_chunked(256 * 1024):
                    size += len(part)
                    if size > mu.MAX_MB * 1024 * 1024:
                        raise ApiError(f"Трек больше {mu.MAX_MB} МБ.")
                    f.write(part)
            try:
                info = await asyncio.get_running_loop().run_in_executor(None, probe, tmp)
            except Exception:  # noqa: BLE001
                info = None
            if not info or not info.audio_streams or info.duration < 5:
                raise ApiError("Это не аудио или трек короче 5 секунд.")
            tmp.replace(dst)
        finally:
            tmp.unlink(missing_ok=True)
        return self._music_json(uid)

    async def music_file(self, request):
        path = await asyncio.get_running_loop().run_in_executor(None, self._track, request)
        return web.FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    async def music_delete(self, request):
        from .. import music as mu
        from .web import ApiError

        if request.match_info["name"].startswith(mu.BUILTIN):
            raise ApiError("Встроенную мелодию удалить нельзя — можно выбрать другую или выключить музыку.")
        self._track(request).unlink(missing_ok=True)
        return self._music_json(request["user"]["id"])

    async def music_prefs(self, request):
        from .. import music as mu

        body = await request.json()
        kw = {k: int(bool(body[k])) for k in ("music_on", "enhance") if k in body}
        if body.get("music_level") in mu.LEVELS:
            kw["music_level"] = body["music_level"]
        if "subs_pos" in body:                 # где наши субтитры: 0 — авто, иначе % высоты от верха
            from ..subtitles import clean_pos

            kw["subs_pos"] = clean_pos(body["subs_pos"])
        if "music_track" in body:
            choice = str(body["music_track"] or "")
            ok = (not choice or choice in {b["id"] for b in mu.builtin_choices()}
                  or choice in {p.name for p in mu.tracks(music_dir(self.s, request["user"]["id"]))})
            if ok:
                kw["music_track"] = choice
        self.db.set_prefs(request["user"]["id"], **kw)
        return self._music_json(request["user"]["id"])

    async def cancel(self, request):
        """Кнопка «Отменить»: задача из очереди снимается, идущая — останавливается; видео остаётся."""
        from .web import ApiError

        job = self._job(request)
        ok, text = self.w.bot.cut.cancel(job)
        if not ok:
            raise ApiError(text)
        return web.json_response(self._json(self.db.cut_job(job["id"])) | {"message": text})

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

    async def share(self, request):
        """«🔗 Ссылка для Claude»: открытая ссылка на видео (исходное или готовое), чтобы отдать файл
        больше лимита чата. Работает CUT_LINK_TTL_H часов или пока файл не удалён."""
        import secrets
        from datetime import timedelta

        from .db import iso, utcnow
        from .web import ApiError

        job = self._job(request)
        if not self.w.bot.public_url:
            raise ApiError("У бота нет публичного адреса — ссылку сделать нельзя.")
        body = await request.json() if request.can_read_body else {}
        what = body.get("what")
        if what not in ("src", "out"):
            what = "out" if self.w.bot.cut.link_valid(job) else "src"
        path = job["out_path"] if what == "out" else job["src_path"]
        if job["status"] == "uploading" or not path or not Path(path).exists():
            raise ApiError("Файла уже нет — загрузи видео заново." if job["status"] != "uploading"
                           else "Дождись окончания загрузки.")
        token = secrets.token_urlsafe(18)
        until = utcnow() + timedelta(hours=self.s.cut_link_ttl_h)
        self.db.update_cut_job(job["id"], share_token=token, share_what=what, share_until=iso(until))
        name = Path(job["filename"]).stem[:60] + ("_result" if what == "out" else "") + ".mp4"
        url = f"{self.w.bot.public_url}/share/{token}/{_quote(name)}"
        try:           # ссылку — и в чат: из Telegram её проще скопировать на телефоне
            await self.w.bot.tg.send(job["user_id"], f"🔗 Ссылка для Claude на {'готовое' if what == 'out' else 'исходное'} "
                                                     f"видео «{job['filename']}» (работает {self.s.cut_link_ttl_h} ч):\n{url}")
        except Exception:  # noqa: BLE001
            pass
        return web.json_response({"url": url, "what": what, "hours": self.s.cut_link_ttl_h,
                                  "size": Path(path).stat().st_size})

    async def shared_file(self, request):
        from .db import iso, utcnow

        job = self.db.cut_job_by_share(request.match_info["token"])
        path = job and (job["out_path"] if job["share_what"] == "out" else job["src_path"])
        if not job or not path or not Path(path).exists() or (job["share_until"] or "") < iso(utcnow()):
            raise web.HTTPNotFound(text="Ссылка устарела или файл уже удалён.")
        return web.FileResponse(path, headers={
            "Content-Type": "video/mp4", "Cache-Control": "no-store", "X-Robots-Tag": "noindex",
            "Content-Disposition": f"inline; filename*=UTF-8''{_quote(request.match_info['name'])}"})

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


def combo_defaults(db, uid):
    """Последний выбор пользователя «что сделать с видео» (или стандартный)."""
    from .. import combo as cb

    try:
        return cb.clean(json.loads(db.prefs(uid)["combo"] or "{}"))
    except (ValueError, TypeError):
        return dict(cb.DEFAULT)


def start_combo(db, job, opts):
    """Ставит задачу «всё сразу» в очередь и запоминает выбор как стандартный."""
    from .. import combo as cb

    db.set_prefs(job["user_id"], combo=json.dumps(opts, ensure_ascii=False))
    db.update_cut_job(job["id"], status="queued", stage="в очереди", progress=0, target=None,
                      target_info=cb.describe(opts), error=None, report=None, mode="combo",
                      options=json.dumps(opts, ensure_ascii=False), ai_state=None, estimate=None)
