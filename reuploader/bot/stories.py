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

from ..smartcut.analyze import Word as sc_word
from ..smartcut.analyze import whisper_transcribe
from ..smartcut.core import cached_transcriber
from ..smartcut.media import probe
from ..story import script as sc
from ..story import eleven
from ..story.assemble import build_story
from .db import iso, utcnow
from .telegram import esc

log = logging.getLogger("stories")
CHUNK = 8 * 1024 * 1024
TG_SEND_MAX_MB = 49
SAFE_NAME = re.compile(r"[^\w.\- ()\[\]а-яА-ЯёЁ]+")
KINDS = ("recap", "theory", "auto", "manual", "chat")
VOICE_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
NO_KEY = ("Нет твоего ключа ElevenLabs — впиши его в панели: «Пересказы и теории» → «Озвучка ElevenLabs»")
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

    def transcriber(self, path_json, progress=None):
        return cached_transcriber(partial(whisper_transcribe, model_size=self.s.whisper_model, progress=progress),
                                  path_json)

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
        from .progress import Reporter

        rep = Reporter(lambda stage, f: self.db.update_story(sid, stage=stage, progress=f), lo=0.05, hi=0.9)
        heard = lambda f: rep("распознаю речь в мультфильме", f)   # noqa: E731
        try:
            words = await loop.run_in_executor(None, self.transcriber(d / "words.json", heard), story["src_path"])
        except Exception as e:  # noqa: BLE001
            log.exception("расшифровка %s", sid)
            return await self._fail(story, f"не удалось распознать речь: {type(e).__name__}: {e}")
        lines = sc.lines_from_words(words)
        duration = story["duration"] or probe(story["src_path"]).duration

        if story["kind"] == "manual":        # свой текст или ответ из чата Claude
            try:
                bodies = sc.parse_chat_answer(story["manual_text"] or "", lines, duration)
            except ValueError as e:
                return await self._fail(story, str(e))
            return await self._deliver(story, bodies)
        if story["kind"] == "chat":          # без API: готовим задание для чата Claude
            self.db.update_story(sid, status="chat", progress=1,
                                 stage="расшифровка готова — скопируй задание в чат Claude")
            await self.bot.tg.send(uid, f"📝 Расшифровка «{esc(story['filename'])}» готова. Открой панель → "
                                        f"«Пересказы и теории» → скопируй задание в чат Claude, а его ответ "
                                        f"вставь обратно.")
            return

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

    def tts_available(self, uid):
        """Озвучка ElevenLabs — только своим ключом пользователя (ключ владельца бота другим не достаётся)."""
        return bool(self.db.eleven(uid)[0])

    async def _deliver(self, story, bodies):
        sid, uid = story["id"], story["user_id"]
        start = len(self.db.story_scripts(sid))
        auto = bool(story["tts"]) and self.tts_available(uid)
        for i, body in enumerate(bodies):
            scid = self.db.add_story_script(sid, start + i, body)
            await self.send_script(self.db.story_script(scid), uid)
            if auto:
                self.request_tts(self.db.story_script(scid), story["tts_voice"])
        self.db.update_story(sid, status="scripts", progress=1,
                             stage=f"сценариев: {len(bodies)} — " + ("озвучиваю через ElevenLabs" if auto else "жду голос"))
        if auto:
            await self.bot.tg.send(uid, "🤖 Озвучиваю сценарии через ElevenLabs и собираю ролики — пришлю сюда.")

    def request_tts(self, item, voice=None):
        """Озвучить сценарий через ElevenLabs (ключом автора ролика) вместо своего голоса. -> (ok, текст)."""
        story = self.db.story(item["story_id"])
        key, saved = self.db.eleven(story["user_id"])
        if not key:
            return False, NO_KEY
        if item["status"] in ("queued", "running"):
            return False, "Этот ролик уже собирается"
        voice = next((v for v in (voice, story["tts_voice"], saved) if v and VOICE_ID.match(v)), None)
        if not voice:
            return False, "Выбери голос в панели: «Пересказы и теории» → «Озвучка ElevenLabs»"
        self.db.update_story_script(item["id"], status="queued", voice_src="tts:" + voice, voice_path=None, error=None)
        self.poke()
        return True, "Озвучиваю и собираю ролик"

    async def send_script(self, item, uid):
        body = json.loads(item["body"])
        buttons = ([[{"text": "🤖 Озвучить через ElevenLabs", "callback_data": f"st_tts:{item['id']}"}]]
                   if self.tts_available(uid) else None)
        msg = await self.bot.tg.send(uid, script_text(body)[:4000], buttons)
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
        self.db.update_story_script(item["id"], status="queued", voice_path=str(dst), voice_src="user", error=None)
        await self.bot.tg.send(chat, "🎙 Голос получил — собираю ролик, пришлю сюда.")
        self.poke()
        return True

    async def render(self, item):
        story = self.db.story(item["story_id"])
        body = json.loads(item["body"])
        d = story_dir(self.s, story["id"])
        out = d / f"out_{item['id']}.mp4"
        self.db.update_story_script(item["id"], status="running", progress=0, stage="начинаю")
        from .progress import Reporter

        rep = Reporter(lambda stage, f: self.db.update_story_script(item["id"], stage=stage, progress=f))
        is_tts = (item["voice_src"] or "").startswith("tts") and not (item["voice_path"] and Path(item["voice_path"]).exists())
        build_rep = rep.sub(0.25 if is_tts else 0.0, 1.0)
        voice_heard = lambda f: build_rep("распознаю голос", 0.35 * f)   # noqa: E731
        tr = partial(whisper_transcribe, model_size=self.s.whisper_model, progress=voice_heard,
                     language=story["lang"] if story["lang"] in ("ru", "en") else None)
        voice_path = item["voice_path"]
        if (item["voice_src"] or "").startswith("tts") and not (voice_path and Path(voice_path).exists()):
            key, saved = self.db.eleven(story["user_id"])
            voice = (item["voice_src"] or "tts:").split(":", 1)[1] or saved
            voice_path = str(d / f"voice_{item['id']}_eleven.wav")
            text = " ".join(ln["text"] for ln in body["lines"])
            rep("озвучиваю через ElevenLabs", 0.02)
            try:
                await asyncio.get_running_loop().run_in_executor(None, partial(
                    eleven.synthesize, text, voice_path, key, voice))
            except Exception as e:  # noqa: BLE001
                log.warning("озвучка %s: %s", item["id"], e)
                self.db.update_story_script(item["id"], status="failed", error=f"озвучка ElevenLabs: {e}"[:500])
                await self.bot.tg.send(story["user_id"], f"❌ Не получилось озвучить «{esc(body['title'])}»: "
                                                         f"{esc(str(e))[:400]}\nМожно ответить на сценарий "
                                                         f"своим голосовым.")
                return
            self.db.update_story_script(item["id"], voice_path=voice_path)
        from .. import music as mu
        from .cutjobs import music_dir, send_video_fit

        prefs = self.db.prefs(story["user_id"])
        track, track_title = (await asyncio.get_running_loop().run_in_executor(
            None, mu.resolve, prefs["music_track"], music_dir(self.s, story["user_id"]), self.s.data_dir / "music_builtin")
            if prefs["music_on"] else (None, None))
        try:
            await asyncio.get_running_loop().run_in_executor(None, partial(
                build_story, story["src_path"], voice_path, body, out, tr, d / f"tmp_{item['id']}", build_rep,
                mirror=bool(story["mirror"]), music=track, music_level=prefs["music_level"],
                loop=bool(story["loop"])))
        except Exception as e:  # noqa: BLE001
            log.exception("сборка %s", item["id"])
            self.db.update_story_script(item["id"], status="failed", error=f"{type(e).__name__}: {e}"[:500])
            await self.bot.tg.send(story["user_id"], f"❌ Не получилось собрать «{esc(body['title'])}»: "
                                                     f"<code>{esc(str(e))[:500]}</code>")
            return
        finally:
            shutil.rmtree(d / f"tmp_{item['id']}", ignore_errors=True)
        token = secrets.token_urlsafe(24)
        self.db.update_story_script(item["id"], status="done", out_path=str(out), dl_token=token, progress=1,
                                    finished_at=iso(utcnow()))
        caption = (f"✅ <b>{esc(body['title'])}</b>\nНазвание для YouTube можно взять это же."
                   + (f"\n🎵 Музыка: {esc(track_title)}" if track_title else ""))
        sent = await send_video_fit(self.bot.tg, story["user_id"], out, caption)
        big = out.stat().st_size > TG_SEND_MAX_MB * 1024 * 1024
        if (big or not sent) and self.bot.public_url:
            await self.bot.tg.send(story["user_id"], ("📥 Полное качество" if sent else caption + "\n📥 Скачать")
                                   + f": {self.bot.public_url}/sdl/{token}")
        elif not sent:
            await self.bot.tg.send(story["user_id"], caption + "\n📥 Не получилось прислать файл — скачай его "
                                                               "в панели («Пересказы и теории» → «Скачать ролик»).")


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
    router.add_post("/api/stories/{sid}/scripts/{scid}/tts", api.tts)
    router.add_get("/api/stories/{sid}/prompt", api.prompt)
    router.add_post("/api/stories/{sid}/prompt/send", api.send_prompt)
    router.add_delete("/api/stories/{sid}", api.delete)
    router.add_get("/sdl/{token}", api.download)
    router.add_get("/api/eleven", api.eleven_get)
    router.add_put("/api/eleven", api.eleven_put)
    router.add_delete("/api/eleven", api.eleven_delete)
    return api


class StoryApi:
    def __init__(self, webapp):
        self.w = webapp
        self.db = webapp.db
        self.s = webapp.s
        self._voices = {}        # uid -> (время, отпечаток ключа, голоса): не дёргать ElevenLabs на каждый показ

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
                            "progress": it["progress"], "stage": it["stage"],
                            "title": body["title"], "kind": body["kind"], "overlay": body["overlay"],
                            "why": body["why"], "text": "\n".join(ln["text"] for ln in body["lines"]),
                            "link": f"/sdl/{it['dl_token']}" if it["dl_token"] else None})
        return {k: st[k] for k in ("id", "status", "stage", "progress", "filename", "size", "duration", "kind",
                                   "lang", "count", "seconds", "topic", "error", "ai_state", "mirror", "loop")} | {
            "uploaded": uploaded, "scripts": scripts,
            "has_transcript": (story_dir(self.s, st["id"]) / "words.json").exists(),
            "estimate": json.loads(st["estimate"]) if st["estimate"] else None}

    async def list(self, request):
        uid = request["user"]["id"]
        return web.json_response({"stories": [self._json(s) for s in self.db.stories(uid)], "chunk": CHUNK,
                                  "max_mb": self.s.story_max_mb, "max_minutes": self.s.story_max_minutes,
                                  "ai_available": self.w.bot.cut.ai_allowed(uid),
                                  "tts_available": self.worker.tts_available(uid)})

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
        text = str(body.get("text") or "").strip()[:30000]
        if kind == "manual" and len(text) < 20:
            raise ApiError("Напиши текст ролика (хотя бы пару предложений).")
        if kind not in ("manual", "chat") and not self.w.bot.cut.ai_allowed(request["user"]["id"]):
            raise ApiError("AI-сценарии доступны владельцу бота. Выбери «Свой текст».")
        voice = str(body.get("tts_voice") or "")
        voice = voice if VOICE_ID.match(voice) else None
        uid = request["user"]["id"]
        self.db.update_story(st["id"], mirror=int(bool(body.get("mirror"))), loop=int(bool(body.get("loop", True))),
                             tts=int(bool(body.get("tts")) and self.worker.tts_available(uid)),
                             tts_voice=voice)
        self.db.update_story(st["id"], status="queued", stage="в очереди", progress=0, kind=kind, lang=lang,
                             count=count, seconds=seconds, topic=str(body.get("topic") or "")[:100] or None,
                             manual_text=text or None, ai_state=None, estimate=None, error=None)
        self.worker.poke()
        return web.json_response(self._json(self.db.story(st["id"])))

    def _chat_prompt(self, st, q):
        from .web import ApiError

        words_path = story_dir(self.s, st["id"]) / "words.json"
        if not words_path.exists():
            raise ApiError("Сначала дождись расшифровки (режим «Через чат Claude»).")
        words = [sc_word(**w) for w in json.loads(words_path.read_text(encoding="utf-8"))]
        lines = sc.lines_from_words(words)
        kind = q.get("kind") if q.get("kind") in ("recap", "theory", "auto") else "auto"
        lang = q.get("lang") if q.get("lang") in ("ru", "en") else "ru"
        try:
            count = max(1, min(5, int(q.get("count", 3))))
            seconds = max(30, min(150, int(q.get("seconds", 60))))
        except ValueError:
            count, seconds = 3, 60
        return sc.chat_prompt(lines, kind, lang, count, seconds, st["duration"] or 0, (q.get("topic") or "")[:100])

    async def prompt(self, request):
        st = self._story(request)
        return web.json_response({"prompt": self._chat_prompt(st, request.query)})

    async def send_prompt(self, request):
        """Задание файлом в Telegram — удобно переслать в чат Claude с телефона."""
        st = self._story(request)
        text = self._chat_prompt(st, await request.json())
        path = story_dir(self.s, st["id"]) / "zadanie_dlya_claude.txt"
        path.write_text(text, encoding="utf-8")
        await self.w.bot.tg.send_document(request["user"]["id"], str(path),
                                          "📝 Задание для Claude: открой claude.ai, приложи этот файл и напиши "
                                          "«выполни задание из файла». Ответ вставь в панели.",
                                          filename="zadanie_dlya_claude.txt", content_type="text/plain")
        return web.json_response({"ok": True})

    async def decide(self, request):
        from .web import ApiError

        st = self._story(request)
        ok, text = self.worker.decide(st, bool((await request.json()).get("yes")))
        if not ok:
            raise ApiError(text)
        return web.json_response(self._json(self.db.story(st["id"])))

    # ---------- личный ключ ElevenLabs ----------
    async def _voices_for(self, uid, key, fresh=False):
        import hashlib
        import time

        mark = hashlib.sha256(key.encode()).hexdigest()[:16]
        hit = self._voices.get(uid)
        if hit and not fresh and hit[1] == mark and time.monotonic() - hit[0] < 600:
            return hit[2]
        vs = await asyncio.get_running_loop().run_in_executor(None, eleven.voices, key)
        self._voices[uid] = (time.monotonic(), mark, vs)
        return vs

    async def _eleven_state(self, uid, fresh=False):
        key, voice = self.db.eleven(uid)
        out = {"has_key": bool(key), "masked": eleven.mask(key) if key else "", "voice": voice,
               "voices": [], "quota": None, "error": None}
        if key:
            try:
                out["voices"] = await self._voices_for(uid, key, fresh)
            except eleven.TTSError as e:
                out["error"] = str(e)
            q = await asyncio.get_running_loop().run_in_executor(None, eleven.quota, key)
            out["quota"] = {"used": q[0], "limit": q[1]} if q else None
        return out

    async def eleven_get(self, request):
        return web.json_response(await self._eleven_state(request["user"]["id"]))

    async def eleven_put(self, request):
        """{"key": "..."} — вписать/заменить свой ключ (проверяется запросом голосов); {"voice": id} — выбрать голос."""
        from .web import ApiError

        uid = request["user"]["id"]
        body = await request.json()
        if body.get("key") is not None:
            key = str(body["key"]).strip()
            if not 20 <= len(key) <= 200 or any(c.isspace() for c in key):
                raise ApiError("Это не похоже на ключ ElevenLabs (он выглядит как sk_… длиной ~50 символов).")
            try:
                vs = await asyncio.get_running_loop().run_in_executor(None, eleven.voices, key)
            except eleven.TTSError as e:
                raise ApiError(f"Ключ не подошёл: {e}") from None
            _, voice = self.db.eleven(uid)
            if not any(v["id"] == voice for v in vs):
                voice = vs[0]["id"] if vs else ""
            self.db.set_eleven(uid, key=key, voice=voice)
            self._voices.pop(uid, None)
        if body.get("voice") is not None:
            voice = str(body["voice"])
            if not VOICE_ID.match(voice):
                raise ApiError("Неизвестный голос.")
            if not self.db.eleven(uid)[0]:
                raise ApiError("Сначала впиши ключ ElevenLabs.")
            self.db.set_eleven(uid, voice=voice)
        return web.json_response(await self._eleven_state(uid))

    async def eleven_delete(self, request):
        uid = request["user"]["id"]
        self.db.set_eleven(uid, key="", voice="")
        self._voices.pop(uid, None)
        return web.json_response(await self._eleven_state(uid))

    async def tts(self, request):
        from .web import ApiError

        st = self._story(request)
        item = self.db.story_script(int(request.match_info["scid"]))
        if not item or item["story_id"] != st["id"]:
            raise ApiError("Не найдено.", status=404)
        if not st["src_path"] or not Path(st["src_path"]).exists():
            raise ApiError("Файл мультфильма удалён — загрузи заново.")
        ok, text = self.worker.request_tts(item, (await request.json()).get("voice"))
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
