"""Пересказы и теории: мультфильм пользователя -> сценарии (Claude или свой текст) ->
голос автора (ответ голосовым в Telegram) -> вертикальный ролик.

Файлы: data/stories/<id>/src.* (мультфильм), words.json (расшифровка),
voice_<script>.* (голос), out_<script>.mp4 (ролик).
Платный запрос к Claude — только после явного «Да» (как в умной обрезке).
"""
import asyncio
import json
import logging
import os
import re
import secrets
import shutil
from functools import partial
from pathlib import Path

from aiohttp import web

from ..smartcut.analyze import whisper_transcribe
from ..smartcut.core import cached_transcriber
from ..smartcut.media import probe
from ..story import script as sc
from ..story.assemble import build_story
from .db import iso, utcnow
from .telegram import esc

log = logging.getLogger("stories")
CHUNK = 8 * 1024 * 1024
TG_SEND_MAX_MB = 49
SAFE_NAME = re.compile(r"[^\w.\- ()\[\]а-яА-ЯёЁ]+")
KINDS = ("recap", "theory", "auto", "manual")
VOICE_HINT = ("🎙 <b>Ответь на это сообщение голосовым</b> — прочитай текст выше своим голосом "
              "(можно своими словами, главное — по порядку). Я соберу ролик: кадры мультфильма под каждую "
              "фразу, твой голос и субтитры.")


def story_dir(settings, sid):
    return settings.story_dir / str(sid)


def model():
    return os.getenv("STORY_AI_MODEL", sc.DEFAULT_MODEL)


def script_text(body):
    kind = sc.KIND_RU.get(body.get("kind"), "")
    head = f"🎬 <b>{esc(body.get('title'))}</b>" + (f" · {kind}" if kind else "")
    extra = []
    if body.get("overlay"):
        extra.append(f"Надпись сверху: «{esc(body['overlay'])}»")
    if body.get("why") and body["why"] != "свой текст":
        extra.append(f"Почему зайдёт: {esc(body['why'])}")
    text = "\n".join(esc(ln["text"]) for ln in body["lines"])
    return head + ("\n<i>" + "\n".join(extra) + "</i>" if extra else "") + f"\n\n{text}\n\n{VOICE_HINT}"


class StoryWorker:
    def __init__(self, db, settings, bot):
        self.db = db
        self.s = settings
        self.bot = bot           # BotApp: tg, cut (ai_allowed, balance), public_url
        self.wake = asyncio.Event()

    def poke(self):
        self.wake.set()

    def transcriber(self, path_json):
        return cached_transcriber(partial(whisper_transcribe, model_size=self.s.whisper_model), path_json)

    async def loop(self):
        self.db.x("UPDATE stories SET status = 'queued' WHERE status = 'running'")
        self.db.x("UPDATE story_scripts SET status = 'queued' WHERE status = 'running'")
        while True:
            try:
                story = self.db.next_story()
                if story:
                    await self.run(story)
                    continue
                item = self.db.next_story_script()
                if item:
                    await self.render(item)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("ошибка очереди пересказов")
            try:
                await asyncio.wait_for(self.wake.wait(), 30)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    # ---------- шаг 1: расшифровка -> оценка / свой текст ----------
    async def run(self, story):
        sid, uid = story["id"], story["user_id"]
        d = story_dir(self.s, sid)
        self.db.update_story(sid, status="running", stage="распознаю речь в мультфильме (долго для фильмов)",
                             progress=0.1, error=None)
        loop = asyncio.get_running_loop()
        try:
            words = await loop.run_in_executor(None, self.transcriber(d / "words.json"), story["src_path"])
        except Exception as e:  # noqa: BLE001
            log.exception("расшифровка %s", sid)
            return await self._fail(story, f"не удалось распознать речь: {type(e).__name__}: {e}")
        lines = sc.lines_from_words(words)
        duration = story["duration"] or probe(story["src_path"]).duration

        if story["kind"] == "manual":
            try:
                body = sc.manual_script(story["manual_text"] or "", lines, duration)
            except ValueError as e:
                return await self._fail(story, str(e))
            return await self._deliver(story, [body])

        if not lines:
            return await self._fail(story, "в мультфильме не нашлось речи — AI не из чего писать сценарий. "
                                           "Выбери «Свой текст».")
        if story["ai_state"] == "confirmed":
            return await self._write(story, lines, duration)
        est = sc.estimate(lines, story["count"], story["seconds"], story["lang"], model(),
                          kind=story["kind"], duration=duration)
        ok, bal = self.bot.cut.balance.can_afford(est["usd"])
        est.update(affordable=ok, remaining=bal["remaining"])
        self.db.update_story(sid, status="confirm", ai_state="awaiting", stage="жду подтверждения",
                             estimate=json.dumps(est), progress=0.3)
        price = f"≈ ${est['usd']:.3f} (≈ {est['rub']:.0f} ₽)"
        if not ok:
            await self.bot.tg.send(uid, f"🎬 Сценарии по «{esc(story['filename'])}» будут стоить {price}, а на "
                                        f"счёте Claude ≈ ${bal['remaining']:.2f}. Не запускаю — можно написать "
                                        f"свой текст в панели («Свой текст»).")
            self.db.update_story(sid, status="failed", ai_state="declined", error="недостаточно средств на счёте Claude")
            return
        await self.bot.tg.send(
            uid, f"🎬 Написать сценарии ({story['count']} шт.) по «{esc(story['filename'])}»? "
                 f"Стоимость примерно {price}, модель {est['model']}.",
            [[{"text": "✅ Да", "callback_data": f"st_yes:{sid}"},
              {"text": "Нет", "callback_data": f"st_no:{sid}"}]])

    def decide(self, story, yes):
        """Ответ пользователя на оценку цены. -> (ok, текст)."""
        if story["status"] != "confirm" or story["ai_state"] != "awaiting":
            return False, "Уже неактуально"
        if not yes:
            self.db.update_story(story["id"], status="uploaded", ai_state="declined", stage="отменено")
            return True, "Отменил. Можно написать свой текст в панели"
        est = json.loads(story["estimate"] or "{}")
        ok, _ = self.bot.cut.balance.can_afford(est.get("usd", 0))
        if not ok or not self.bot.cut.ai_allowed(story["user_id"]):
            return False, "Недостаточно средств на счёте Claude"
        self.db.update_story(story["id"], status="queued", ai_state="confirmed", stage="в очереди (AI)")
        self.poke()
        return True, "Пишу сценарии"

    async def _write(self, story, lines, duration):
        import anthropic

        sid, uid = story["id"], story["user_id"]
        # Платный запрос — только после «Да» (ai_state == confirmed) и только тем, кому разрешён AI
        if not self.bot.cut.ai_allowed(uid):
            return await self._fail(story, "AI-режим недоступен")
        self.db.update_story(sid, status="running", stage="пишу сценарии (Claude)", progress=0.6)
        try:
            scripts, note = await asyncio.get_running_loop().run_in_executor(None, partial(
                sc.write_scripts, anthropic.Anthropic(), lines, "auto" if story["kind"] == "auto" else story["kind"],
                story["lang"], story["count"], story["seconds"], duration, story["topic"] or "", model(),
                usage_log=lambda u: self.db.add_ai_usage(uid, None, u)))
        except Exception as e:  # noqa: BLE001
            log.exception("сценарии %s", sid)
            return await self._fail(story, f"Claude: {type(e).__name__}: {e}")
        if not scripts:
            return await self._fail(story, note or "сценариев нет")
        await self._deliver(story, scripts)

    async def _deliver(self, story, bodies):
        sid, uid = story["id"], story["user_id"]
        start = len(self.db.story_scripts(sid))
        for i, body in enumerate(bodies):
            scid = self.db.add_story_script(sid, start + i, body)
            await self.send_script(self.db.story_script(scid), uid)
        self.db.update_story(sid, status="scripts", stage=f"сценариев: {len(bodies)} — жду голос", progress=1)

    async def send_script(self, item, uid):
        body = json.loads(item["body"])
        msg = await self.bot.tg.send(uid, script_text(body)[:4000])
        if msg:
            self.db.update_story_script(item["id"], tg_chat=uid, tg_message_id=msg["message_id"])
        return msg

    async def _fail(self, story, error):
        self.db.update_story(story["id"], status="failed", stage="ошибка", error=error[:500])
        await self.bot.tg.send(story["user_id"], f"❌ «{esc(story['filename'])}»: {esc(error)[:700]}")

    # ---------- шаг 2: голос -> ролик ----------
    async def on_voice(self, msg):
        """Голосовое/аудио в ответ на сценарий. -> True, если это был ответ на сценарий."""
        reply = msg.get("reply_to_message") or {}
        chat = msg["chat"]["id"]
        item = self.db.story_script_by_message(chat, reply.get("message_id"))
        if not item:
            return False
        story = self.db.story(item["story_id"])
        media = (msg.get("voice") or msg.get("audio") or msg.get("document") or msg.get("video_note")
                 or msg.get("video"))
        if not story or not media:
            return False
        if item["status"] in ("queued", "running"):
            await self.bot.tg.send(chat, "Этот ролик уже собирается — подожди немного.")
            return True
        if not story["src_path"] or not Path(story["src_path"]).exists():
            await self.bot.tg.send(chat, "Файл мультфильма уже удалён — загрузи его заново в панели.")
            return True
        if (media.get("file_size") or 0) > 20 * 1024 * 1024:
            await self.bot.tg.send(chat, "Запись больше 20 МБ — Telegram не даст её скачать. Запиши короче "
                                         "или голосовым сообщением.")
            return True
        ext = Path(media.get("file_name") or "voice.ogg").suffix or ".ogg"
        dst = story_dir(self.s, story["id"]) / f"voice_{item['id']}{ext}"
        await self.bot.tg.download(media["file_id"], dst)
        self.db.update_story_script(item["id"], status="queued", voice_path=str(dst), error=None)
        await self.bot.tg.send(chat, "🎙 Голос получил — собираю ролик, пришлю сюда.")
        self.poke()
        return True

    async def render(self, item):
        story = self.db.story(item["story_id"])
        body = json.loads(item["body"])
        d = story_dir(self.s, story["id"])
        out = d / f"out_{item['id']}.mp4"
        self.db.update_story_script(item["id"], status="running")
        tr = partial(whisper_transcribe, model_size=self.s.whisper_model,
                     language=story["lang"] if story["lang"] in ("ru", "en") else None)
        try:
            await asyncio.get_running_loop().run_in_executor(None, partial(
                build_story, story["src_path"], item["voice_path"], body, out, tr, d / f"tmp_{item['id']}"))
        except Exception as e:  # noqa: BLE001
            log.exception("сборка %s", item["id"])
            self.db.update_story_script(item["id"], status="failed", error=f"{type(e).__name__}: {e}"[:500])
            await self.bot.tg.send(story["user_id"], f"❌ Не получилось собрать «{esc(body['title'])}»: "
                                                     f"<code>{esc(str(e))[:500]}</code>")
            return
        finally:
            shutil.rmtree(d / f"tmp_{item['id']}", ignore_errors=True)
        token = secrets.token_urlsafe(24)
        self.db.update_story_script(item["id"], status="done", out_path=str(out), dl_token=token,
                                    finished_at=iso(utcnow()))
        caption = f"✅ <b>{esc(body['title'])}</b>\nНазвание для YouTube можно взять это же."
        sent = False
        if out.stat().st_size <= TG_SEND_MAX_MB * 1024 * 1024:
            try:
                await self.bot.tg.send_video(story["user_id"], str(out), caption, 1080, 1920)
                sent = True
            except Exception as e:  # noqa: BLE001
                log.warning("не смог отправить ролик: %s", e)
        if not sent and self.bot.public_url:
            await self.bot.tg.send(story["user_id"], caption + f"\n📥 {self.bot.public_url}/sdl/{token}")


# ---------------- API мини-апки ----------------

def setup(webapp, router):
    api = StoryApi(webapp)
    router.add_get("/api/stories", api.list)
    router.add_post("/api/stories", api.create)
    router.add_get("/api/stories/{sid}", api.get)
    router.add_put("/api/stories/{sid}/chunk", api.chunk)
    router.add_post("/api/stories/{sid}/run", api.run)
    router.add_post("/api/stories/{sid}/ai", api.decide)
    router.add_post("/api/stories/{sid}/scripts/{scid}/send", api.resend)
    router.add_delete("/api/stories/{sid}", api.delete)
    router.add_get("/sdl/{token}", api.download)
    return api


class StoryApi:
    def __init__(self, webapp):
        self.w = webapp
        self.db = webapp.db
        self.s = webapp.s

    @property
    def worker(self):
        return self.w.bot.stories

    def _story(self, request):
        from .web import ApiError

        try:
            st = self.db.story(int(request.match_info["sid"]))
        except ValueError:
            st = None
        if not st or st["user_id"] != request["user"]["id"]:
            raise ApiError("Не найдено.", status=404)
        return st

    def _json(self, st):
        uploaded = 0
        if st["status"] == "uploading" and st["src_path"] and Path(st["src_path"]).exists():
            uploaded = Path(st["src_path"]).stat().st_size
        scripts = []
        for it in self.db.story_scripts(st["id"]):
            body = json.loads(it["body"])
            scripts.append({"id": it["id"], "status": it["status"], "error": it["error"],
                            "title": body["title"], "kind": body["kind"], "overlay": body["overlay"],
                            "why": body["why"], "text": "\n".join(ln["text"] for ln in body["lines"]),
                            "link": f"/sdl/{it['dl_token']}" if it["dl_token"] else None})
        return {k: st[k] for k in ("id", "status", "stage", "progress", "filename", "size", "duration", "kind",
                                   "lang", "count", "seconds", "topic", "error", "ai_state")} | {
            "uploaded": uploaded, "scripts": scripts,
            "estimate": json.loads(st["estimate"]) if st["estimate"] else None}

    async def list(self, request):
        uid = request["user"]["id"]
        return web.json_response({"stories": [self._json(s) for s in self.db.stories(uid)], "chunk": CHUNK,
                                  "max_mb": self.s.story_max_mb, "max_minutes": self.s.story_max_minutes,
                                  "ai_available": self.w.bot.cut.ai_allowed(uid)})

    async def get(self, request):
        return web.json_response(self._json(self._story(request)))

    async def create(self, request):
        from .web import ApiError

        body = await request.json()
        size = int(body.get("size") or 0)
        name = SAFE_NAME.sub("_", str(body.get("filename") or "cartoon.mp4"))[:120]
        if size <= 0:
            raise ApiError("Пустой файл.")
        if size > self.s.story_max_mb * 1024 * 1024:
            raise ApiError(f"Файл больше {self.s.story_max_mb} МБ.")
        sid = self.db.create_story(request["user"]["id"], name, size)
        d = story_dir(self.s, sid)
        d.mkdir(parents=True, exist_ok=True)
        src = d / ("src" + (Path(name).suffix.lower() or ".mp4"))
        src.touch()
        self.db.update_story(sid, src_path=str(src))
        return web.json_response(self._json(self.db.story(sid)) | {"chunk": CHUNK})

    async def chunk(self, request):
        from .web import ApiError

        st = self._story(request)
        if st["status"] != "uploading":
            raise ApiError("Загрузка уже завершена.")
        src = Path(st["src_path"])
        offset = int(request.query.get("offset", "0"))
        have = src.stat().st_size
        if offset != have:
            return web.json_response({"uploaded": have}, status=409)
        written = 0
        with open(src, "ab") as f:
            async for part in request.content.iter_chunked(256 * 1024):
                written += len(part)
                if written > CHUNK + 1024 or have + written > st["size"]:
                    raise ApiError("Слишком большой кусок.")
                f.write(part)
        have += written
        if have >= st["size"]:
            try:
                info = await asyncio.get_running_loop().run_in_executor(None, probe, src)
            except Exception:  # noqa: BLE001
                self.db.update_story(st["id"], status="failed", error="это не видео или файл повреждён")
                raise ApiError("Это не видео или файл повреждён.") from None
            if info.duration > self.s.story_max_minutes * 60:
                self.db.update_story(st["id"], status="failed", error="слишком длинное видео")
                raise ApiError(f"Видео длиннее {self.s.story_max_minutes} минут.")
            self.db.update_story(st["id"], status="uploaded", duration=info.duration,
                                 stage=f"длина {int(info.duration // 60)}:{int(info.duration % 60):02d}")
        return web.json_response(self._json(self.db.story(st["id"])))

    async def run(self, request):
        """{"kind": recap|theory|auto|manual, "lang": ru|en, "count": 1..5, "seconds": 30..150, "topic", "text"}"""
        from .web import ApiError

        st = self._story(request)
        if st["status"] in ("uploading", "queued", "running", "confirm"):
            raise ApiError("Сначала дождись окончания загрузки." if st["status"] == "uploading"
                           else "Уже в работе.")
        if not st["src_path"] or not Path(st["src_path"]).exists():
            raise ApiError("Файл удалён — загрузи заново.")
        body = await request.json()
        kind = body.get("kind", "auto")
        if kind not in KINDS:
            raise ApiError("Неизвестный вид роликов.")
        lang = body.get("lang", "ru") if body.get("lang") in ("ru", "en") else "ru"
        try:
            count = max(1, min(5, int(body.get("count", 3))))
            seconds = max(30, min(150, int(body.get("seconds", 60))))
        except (TypeError, ValueError):
            raise ApiError("Число роликов и длина — числами.") from None
        text = str(body.get("text") or "").strip()[:6000]
        if kind == "manual" and len(text) < 20:
            raise ApiError("Напиши текст ролика (хотя бы пару предложений).")
        if kind != "manual" and not self.w.bot.cut.ai_allowed(request["user"]["id"]):
            raise ApiError("AI-сценарии доступны владельцу бота. Выбери «Свой текст».")
        self.db.update_story(st["id"], status="queued", stage="в очереди", progress=0, kind=kind, lang=lang,
                             count=count, seconds=seconds, topic=str(body.get("topic") or "")[:100] or None,
                             manual_text=text or None, ai_state=None, estimate=None, error=None)
        self.worker.poke()
        return web.json_response(self._json(self.db.story(st["id"])))

    async def decide(self, request):
        from .web import ApiError

        st = self._story(request)
        ok, text = self.worker.decide(st, bool((await request.json()).get("yes")))
        if not ok:
            raise ApiError(text)
        return web.json_response(self._json(self.db.story(st["id"])))

    async def resend(self, request):
        from .web import ApiError

        st = self._story(request)
        item = self.db.story_script(int(request.match_info["scid"]))
        if not item or item["story_id"] != st["id"]:
            raise ApiError("Не найдено.", status=404)
        await self.worker.send_script(item, request["user"]["id"])
        return web.json_response({"ok": True})

    async def delete(self, request):
        from .web import ApiError

        st = self._story(request)
        if st["status"] in ("queued", "running"):
            raise ApiError("Уже в работе — подожди.")
        shutil.rmtree(story_dir(self.s, st["id"]), ignore_errors=True)
        self.db.x("DELETE FROM story_scripts WHERE story_id = ?", st["id"])
        self.db.x("DELETE FROM stories WHERE id = ?", st["id"])
        return web.json_response({"ok": True})

    async def download(self, request):
        item = self.db.story_script_by_token(request.match_info["token"])
        if not item or not item["out_path"] or not Path(item["out_path"]).exists():
            raise web.HTTPNotFound(text="Файл не найден.")
        return web.FileResponse(item["out_path"], headers={
            "Content-Disposition": "attachment; filename=short.mp4", "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex"})
