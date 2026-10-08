"""Очередь «умной обрезки» и «замены субтитров»: одна задача за раз, прогресс в Telegram, выдача результата.

Файлы лежат в data/cut/<id>/: src.* (исходник) и out.mp4 (результат).
Исходник удаляется сразу после обработки; результат — после отправки в Telegram
или, если он больше лимита Telegram, по истечении срока ссылки на скачивание.
"""
import asyncio
import json
import logging
import secrets
import shutil
from datetime import timedelta
from functools import partial
from pathlib import Path

import os

from ..smartcut import CutError, format_report, smart_cut
from ..smartcut import ai as smart_ai
from ..smartcut.analyze import whisper_transcribe
from ..smartcut.core import cached_transcriber, prepare_beats
from .balance import Balance, ai_enabled
from .db import from_iso, iso, utcnow
from .telegram import esc

log = logging.getLogger("cut")
TG_SEND_MAX_MB = 49          # больше — отдаём ссылкой
PROGRESS_EVERY = 5           # как часто обновлять сообщение с прогрессом, сек


def job_dir(settings, jid):
    return settings.cut_dir / str(jid)


def bar(frac, width=12):
    n = int(round(frac * width))
    return "▓" * n + "░" * (width - n)


SUBS_MODES = {"subs": "erase", "subs_strip": "strip", "subs_crop": "crop"}     # режим задачи -> способ
SUBS_MODE_RU = {"erase": "старые субтитры стёр (картинку под ними дорисовал), наши — на их месте",
                "strip": "старые субтитры закрыл размытой полоской, наши — поверх неё",
                "crop": "старые субтитры убрал — обрезал полосу с ними",
                "blur": "старые субтитры размыл (они лежали поверх картинки)",
                "clean": "старых субтитров не нашёл"}


async def send_video_fit(tg, chat, path, caption):
    """Шлёт видео в Telegram; если файл больше лимита ботов (50 МБ) — шлёт пережатую копию,
    оригинал не трогает. -> True, если отправилось."""
    from ..effects import shrink_to
    from ..smartcut.media import probe

    path = Path(path)
    send = path
    try:
        if path.stat().st_size > TG_SEND_MAX_MB * 1024 * 1024:
            send = path.with_name(path.stem + "_tg.mp4")
            shutil.copyfile(path, send)
            loop = asyncio.get_running_loop()
            dur = (await loop.run_in_executor(None, probe, path)).duration
            await loop.run_in_executor(None, shrink_to, send, TG_SEND_MAX_MB - 2, dur)
        await tg.send_video(chat, str(send), caption)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("не смог отправить видео: %s", e)
        return False
    finally:
        if send != path:
            send.unlink(missing_ok=True)


def music_dir(settings, uid):
    return settings.data_dir / "music" / str(uid)


class Cancelled(Exception):
    """Пользователь нажал «Отменить»."""


def cancel_button(jid):
    return [[{"text": "⏹ Отменить", "callback_data": f"cancel:{jid}"}]]


def pos_text(pos):
    return f", на {pos}% от верха" if pos else ""


def report_text(report):
    """Отчёт задачи для чата и панели."""
    if report.get("kind") == "combo":
        from ..combo import FRAMES

        o = report["options"]
        lines = [f"🖼 Кадр: {FRAMES.get(o['frame'], o['frame'])}"]
        lines.append(f"🔤 Наши субтитры: {report.get('words', 0)} слов{pos_text(report.get('subs_pos'))}"
                     if o["subs"] else "🔤 Без наших субтитров")
        if o["uniq"]:
            u = report.get("uniq") or {}
            lines.append(f"✨ Уникализация: зум {u.get('zoom')}, наклон {u.get('rotate_deg')}°, "
                         f"скорость ×{u.get('tempo') or 1}")
        t = report.get("trim")
        if t and t.get("status") == "failed":
            lines.append(f"✂️ Длину не менял: {t.get('why', '')}")
        elif t:
            lines.append(f"✂️ Длина: {t['before']:.0f} с → {t['after']:.0f} с" if t.get("after") else
                         "✂️ Ролик уже короче — длину не менял")
        lp = report.get("loop")
        if lp:
            what = "конец перетекает в начало"
            if lp.get("dropped"):
                what += f", убрал концовку «{lp['dropped'][:80]}»"
            elif lp.get("cut", 0) >= 0.5:
                what += f", убрал {lp['cut']:.1f} с тишины в конце"
            lines.append(f"🔁 Петля: {what}")
        if report.get("music"):
            lines.append(f"🎵 Музыка: {report['music']}")
        return "\n".join(lines)
    if report.get("kind") == "subs":
        text = (f"🔤 Субтитры заменены: {SUBS_MODE_RU.get(report['mode'], report['mode'])}; "
                f"наши — по речи ({report['words']} слов{pos_text(report.get('subs_pos'))}).")
        if report.get("music"):
            text += f"\n🎵 Фоновая музыка: {report['music']} (приглушается, когда говорят)."
        return text
    return format_report(report)


def ai_model():
    return os.getenv("CUT_AI_MODEL", smart_ai.DEFAULT_MODEL)


class CutWorker:
    def __init__(self, db, settings, bot):
        self.db = db
        self.s = settings
        self.bot = bot           # BotApp: tg, app_button, public_url, owner_id
        self.balance = Balance(db)
        self.wake = asyncio.Event()
        self.stop = set()        # задачи, которые попросили отменить (проверяется в progress)

    def ai_allowed(self, uid):
        """AI-режим тратит деньги владельца: по умолчанию только ему (CUT_AI_FOR_ALL=1 — всем)."""
        return ai_enabled() and (uid == self.bot.owner_id or os.getenv("CUT_AI_FOR_ALL") == "1")

    def transcriber(self, jid):
        return cached_transcriber(partial(whisper_transcribe, model_size=self.s.whisper_model),
                                  job_dir(self.s, jid) / "words.json")

    def poke(self):
        self.wake.set()

    async def loop(self):
        # задачи, прерванные перезапуском бота, — снова в очередь
        self.db.x("UPDATE cut_jobs SET status = 'queued', stage = 'в очереди' WHERE status = 'running'")
        while True:
            try:
                await self.balance.refresh_admin()
                if self.bot.owner_id:
                    await self.balance.check_low(lambda text: self.bot.tg.send(self.bot.owner_id, text))
                await self.cleanup()
                job = self.db.next_cut_job()
                if job:
                    await self.run(job)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("ошибка очереди обрезки")
            try:
                await asyncio.wait_for(self.wake.wait(), 30)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def run(self, job):
        self.stop.discard(job["id"])           # старая отмена (после которой задачу запустили заново) не в счёт
        try:
            if job["mode"] == "combo":
                return await self.combo(job)
            if job["mode"] in SUBS_MODES:
                return await self.resub(job)
            if job["mode"] == "ai" and job["ai_state"] != "confirmed":
                return await self.estimate(job)
            return await self.cut(job)
        finally:
            self.stop.discard(job["id"])

    async def estimate(self, job):
        """AI-режим, шаг 1: локальная разметка + оценка цены. Никаких платных запросов."""
        jid, uid = job["id"], job["user_id"]
        self.db.update_cut_job(jid, status="running", stage="оцениваю стоимость AI-анализа", progress=0.05)
        try:
            _, beats = await asyncio.get_running_loop().run_in_executor(
                None, partial(prepare_beats, job["src_path"], self.transcriber(jid), job_dir(self.s, jid) / "tmp"))
            est = smart_ai.estimate(beats, ai_model())
        except Exception as e:  # noqa: BLE001
            log.exception("оценка %s", jid)
            return await self._fail(job, f"{type(e).__name__}: {e}", None)
        finally:
            shutil.rmtree(job_dir(self.s, jid) / "tmp", ignore_errors=True)
        ok, bal = self.balance.can_afford(est["usd"])
        est["affordable"] = ok
        est["remaining"] = bal["remaining"]
        self.db.update_cut_job(jid, status="confirm", ai_state="awaiting", stage="жду подтверждения",
                               estimate=json.dumps(est))
        price = f"≈ ${est['usd']:.3f} (≈ {est['rub']:.0f} ₽)"
        if not ok:
            text = (f"🤖 AI-анализ «{esc(job['filename'])}» будет стоить {price}, а на счёте Claude "
                    f"≈ ${bal['remaining']:.2f}. Не запускаю — могу обрезать бесплатно.")
            buttons = [[{"text": "✂️ Бесплатный режим", "callback_data": f"ai_no:{jid}"}]]
        else:
            text = (f"🤖 AI-анализ «{esc(job['filename'])}» будет стоить примерно {price} "
                    f"({est['input_tokens']:,} + {est['output_tokens']:,} токенов, {est['model']}).\n"
                    f"Продолжить?").replace(",", " ")
            buttons = [[{"text": "✅ Да", "callback_data": f"ai_yes:{jid}"},
                        {"text": "Нет, бесплатный режим", "callback_data": f"ai_no:{jid}"}]]
        await self.bot.tg.send(uid, text, buttons)

    def decide_ai(self, job, yes):
        """Решение пользователя по AI-режиму. -> (ok, текст)."""
        if job["status"] != "confirm" or job["ai_state"] != "awaiting":
            return False, "Уже неактуально"
        if yes:
            est = json.loads(job["estimate"] or "{}")
            ok, bal = self.balance.can_afford(est.get("usd", 0))
            if not ok or not self.ai_allowed(job["user_id"]):
                return False, "Недостаточно средств на счёте Claude — выбери бесплатный режим"
            self.db.update_cut_job(job["id"], status="queued", ai_state="confirmed", stage="в очереди (AI)")
        else:
            self.db.update_cut_job(job["id"], status="queued", mode="free", ai_state="declined",
                                   stage="в очереди")
        self.poke()
        return True, "Запускаю AI-анализ" if yes else "Режу бесплатно"

    async def cut(self, job):
        jid, uid = job["id"], job["user_id"]
        self.db.update_cut_job(jid, status="running", stage="старт", progress=0)
        msg = await self.bot.tg.send(uid, f"✂️ Обрезаю «{esc(job['filename'])}»…\n{bar(0)} 0%", cancel_button(jid))
        if msg:
            self.db.update_cut_job(jid, tg_message_id=msg["message_id"])

        state = {"stage": "старт", "frac": 0.0}

        def progress(stage, frac):          # вызывается из рабочего потока
            if jid in self.stop:            # нажали «Отменить» — прерываем работу в этом потоке
                raise Cancelled()
            state.update(stage=stage, frac=frac)
            self.db.update_cut_job(jid, stage=stage, progress=round(frac, 3))

        async def ticker():
            last = None
            while True:
                await asyncio.sleep(PROGRESS_EVERY)
                cur = (state["stage"], int(state["frac"] * 100))
                if msg and cur != last:
                    last = cur
                    await self._edit(uid, msg["message_id"],
                                     f"✂️ Обрезаю «{esc(job['filename'])}»…\n{bar(state['frac'])} {cur[1]}% — {cur[0]}",
                                     cancel_button(jid))

        out = job_dir(self.s, jid) / "out.mp4"
        scorer = None
        # Платный запрос возможен ТОЛЬКО после явного «Да» пользователя (ai_state == confirmed)
        if job["mode"] == "ai" and job["ai_state"] == "confirmed" and self.ai_allowed(uid):
            import anthropic

            scorer = smart_ai.make_scorer(
                anthropic.Anthropic(), ai_model(),
                usage_log=lambda u: self.db.add_ai_usage(uid, jid, u))
        tick = asyncio.create_task(ticker())
        try:
            report = await asyncio.get_running_loop().run_in_executor(None, partial(
                smart_cut, job["src_path"], out, job["target"], tolerance=job["tolerance"] or 0.05,
                progress=progress,
                transcriber=self.transcriber(jid), scorer=scorer,
                work_dir=job_dir(self.s, jid) / "tmp"))
        except CutError as e:
            if jid in self.stop:
                return await self._cancelled(job, msg)
            await self._fail(job, str(e), msg)
            return
        except Exception as e:  # noqa: BLE001
            if jid in self.stop:
                return await self._cancelled(job, msg)
            log.exception("обрезка %s", jid)
            await self._fail(job, f"{type(e).__name__}: {e}", msg)
            return
        finally:
            tick.cancel()
            shutil.rmtree(job_dir(self.s, jid) / "tmp", ignore_errors=True)
            if jid not in self.stop:          # после отмены исходник оставляем — можно выбрать заново
                Path(job["src_path"]).unlink(missing_ok=True)      # исходник больше не нужен

        text = format_report(report)
        if report["status"] == "already_short":
            self.db.update_cut_job(jid, status="done", stage="готово", progress=1, report=json.dumps(report),
                                   finished_at=iso(utcnow()), delete_at=iso(utcnow()))
            if msg:
                await self._edit(uid, msg["message_id"], "✂️ " + esc(text))
            return

        await self._deliver(job, msg, out, report, f"✂️ {esc(job['filename'])}\n"
                                                    f"{report['before']:.0f} с → {report['after']:.0f} с")

    async def resub(self, job):
        """Замена вшитых субтитров: старые убрать, наши анимированные — по речи."""
        from ..resub import replace_subtitles

        jid, uid = job["id"], job["user_id"]
        self.db.update_cut_job(jid, status="running", stage="старт", progress=0)
        head = f"🔤 Меняю субтитры в «{esc(job['filename'])}»…"
        msg = await self.bot.tg.send(uid, f"{head}\n{bar(0)} 0%", cancel_button(jid))
        if msg:
            self.db.update_cut_job(jid, tg_message_id=msg["message_id"])
        state = {"stage": "старт", "frac": 0.0}

        def progress(stage, frac):
            if jid in self.stop:            # нажали «Отменить» — прерываем работу в этом потоке
                raise Cancelled()
            state.update(stage=stage, frac=frac)
            self.db.update_cut_job(jid, stage=stage, progress=round(frac, 3))

        async def ticker():
            last = None
            while True:
                await asyncio.sleep(PROGRESS_EVERY)
                cur = (state["stage"], int(state["frac"] * 100))
                if msg and cur != last:
                    last = cur
                    await self._edit(uid, msg["message_id"], f"{head}\n{bar(state['frac'])} {cur[1]}% — {cur[0]}",
                                     cancel_button(jid))

        out = job_dir(self.s, jid) / "out.mp4"
        from .. import music as mu

        prefs = self.db.prefs(uid)
        track, title = (await asyncio.get_running_loop().run_in_executor(
            None, mu.resolve, prefs["music_track"], music_dir(self.s, uid), self.s.data_dir / "music_builtin")
            if prefs["music_on"] else (None, None))
        tick = asyncio.create_task(ticker())
        try:
            report = await asyncio.get_running_loop().run_in_executor(None, partial(
                replace_subtitles, job["src_path"], out, self.transcriber(jid), job_dir(self.s, jid) / "tmp",
                progress, SUBS_MODES[job["mode"]], track, prefs["music_level"], False,
                subs_pos=prefs["subs_pos"]))
        except Exception as e:  # noqa: BLE001
            if jid in self.stop:
                return await self._cancelled(job, msg)
            log.exception("субтитры %s", jid)
            await self._fail(job, f"{type(e).__name__}: {e}", msg, "заменить субтитры в")
            return
        finally:
            tick.cancel()
            shutil.rmtree(job_dir(self.s, jid) / "tmp", ignore_errors=True)
            if jid not in self.stop:          # после отмены исходник оставляем — можно выбрать заново
                Path(job["src_path"]).unlink(missing_ok=True)
        report["kind"] = "subs"
        report["music"] = title
        await self._deliver(job, msg, out, report, f"🔤 {esc(job['filename'])}")

    async def combo(self, job):
        """«Всё сразу»: длина -> уникализация -> кадр + наши субтитры + музыка (см. reuploader.combo)."""
        from .. import combo as cb
        from .. import music as mu

        jid, uid = job["id"], job["user_id"]
        try:
            opts = cb.clean(json.loads(job["options"] or "{}"))
        except ValueError as e:
            await self._fail(job, str(e), None, "сделать")
            return
        self.db.update_cut_job(jid, status="running", stage="старт", progress=0)
        head = f"🚀 Делаю «{esc(job['filename'])}»…"
        msg = await self.bot.tg.send(uid, f"{head}\n{bar(0)} 0%", cancel_button(jid))
        if msg:
            self.db.update_cut_job(jid, tg_message_id=msg["message_id"])
        state = {"stage": "старт", "frac": 0.0}

        def progress(stage, frac):
            if jid in self.stop:            # нажали «Отменить» — прерываем работу в этом потоке
                raise Cancelled()
            state.update(stage=stage, frac=frac)
            self.db.update_cut_job(jid, stage=stage, progress=round(frac, 3))

        async def ticker():
            last = None
            while True:
                await asyncio.sleep(PROGRESS_EVERY)
                cur = (state["stage"], int(state["frac"] * 100))
                if msg and cur != last:
                    last = cur
                    await self._edit(uid, msg["message_id"], f"{head}\n{bar(state['frac'])} {cur[1]}% — {cur[0]}",
                                     cancel_button(jid))

        prefs = self.db.prefs(uid)
        track, title = (await asyncio.get_running_loop().run_in_executor(
            None, mu.resolve, prefs["music_track"], music_dir(self.s, uid), self.s.data_dir / "music_builtin")
            if prefs["music_on"] else (None, None))

        def make_tr(cache):
            return cached_transcriber(partial(whisper_transcribe, model_size=self.s.whisper_model), cache)

        out = job_dir(self.s, jid) / "out.mp4"
        tick = asyncio.create_task(ticker())
        try:
            report = await asyncio.get_running_loop().run_in_executor(None, partial(
                cb.run, job["src_path"], out, opts, make_tr, job_dir(self.s, jid) / "tmp", progress,
                track, prefs["music_level"], prefs["subs_pos"]))
        except Exception as e:  # noqa: BLE001
            if jid in self.stop:
                return await self._cancelled(job, msg)
            log.exception("всё сразу %s", jid)
            await self._fail(job, f"{type(e).__name__}: {e}", msg, "сделать")
            return
        finally:
            tick.cancel()
            shutil.rmtree(job_dir(self.s, jid) / "tmp", ignore_errors=True)
            if jid not in self.stop:          # после отмены исходник оставляем — можно выбрать заново
                Path(job["src_path"]).unlink(missing_ok=True)
        report["music"] = title
        await self._deliver(job, msg, out, report, f"🚀 {esc(job['filename'])}")

    async def _deliver(self, job, msg, out, report, caption):
        """Готовый файл: в Telegram (до лимита) или ссылкой на скачивание."""
        jid, uid = job["id"], job["user_id"]
        text = report_text(report)
        token = secrets.token_urlsafe(24)
        ttl = timedelta(hours=self.s.cut_link_ttl_h)
        self.db.update_cut_job(jid, status="done", stage="готово", progress=1, out_path=str(out),
                               report=json.dumps(report, ensure_ascii=False), dl_token=token,
                               finished_at=iso(utcnow()), delete_at=iso(utcnow() + ttl))
        if msg:
            await self._edit(uid, msg["message_id"], f"✅ Готово: «{esc(job['filename'])}»")
        size_mb = out.stat().st_size / 1024 / 1024
        link = f"{self.bot.public_url}/dl/{token}" if self.bot.public_url else None
        sent = await send_video_fit(self.bot.tg, uid, out, caption)
        tail = ""
        if link and (not sent or size_mb > TG_SEND_MAX_MB):
            tail = (f"\n\n📥 Полное качество ({size_mb:.0f} МБ) — по ссылке, действует "
                    f"{self.s.cut_link_ttl_h} ч:\n{link}")
        elif not sent:
            tail = "\n\n📥 Не получилось прислать файл в Telegram — скачай его в панели (кнопка «Скачать»)."
        await self.bot.tg.send(uid, esc(text)[:3900] + tail)
        # файл остаётся до конца срока ссылки — его можно скачать из панели и позже

    async def _fail(self, job, error, msg, what="обрезать"):
        self.db.update_cut_job(job["id"], status="failed", error=error[:500], finished_at=iso(utcnow()),
                               delete_at=iso(utcnow()))
        text = f"❌ Не получилось {what} «{esc(job['filename'])}»:\n<code>{esc(error)[:700]}</code>"
        if msg:
            await self._edit(job["user_id"], msg["message_id"], text)
        else:
            await self.bot.tg.send(job["user_id"], text)

    def cancel(self, job):
        """Кнопка «Отменить». В очереди — снимаем сразу, в работе — останавливаем при ближайшей
        проверке (обычно за секунду). Видео не удаляется: можно сразу выбрать другой вариант.
        -> (ok, текст)."""
        if job["status"] in ("queued", "confirm"):
            self.db.update_cut_job(job["id"], status="uploaded", stage="отменено", progress=0, error=None)
            return True, "Отменил — видео осталось, можно выбрать заново"
        if job["status"] == "running":
            self.stop.add(job["id"])
            return True, "Останавливаю…"
        return False, "Уже нечего отменять"

    async def _cancelled(self, job, msg):
        jid = job["id"]
        (job_dir(self.s, jid) / "out.mp4").unlink(missing_ok=True)
        self.db.update_cut_job(jid, status="uploaded", stage="отменено", progress=0, error=None, report=None)
        text = f"⏹ Отменил «{esc(job['filename'])}». Видео осталось — можно выбрать заново."
        if msg:
            await self._edit(job["user_id"], msg["message_id"], text)
        offer = getattr(self.bot, "offer_video", None)
        if offer and job["src_path"] and Path(job["src_path"]).exists():
            await offer(job["user_id"], jid, job["filename"])

    async def _edit(self, chat, message_id, text, buttons=None):
        try:
            await self.bot.tg.call("editMessageText", chat_id=chat, message_id=message_id,
                                   text=text, parse_mode="HTML",
                                   **({"reply_markup": {"inline_keyboard": buttons}} if buttons else {}))
        except Exception:  # noqa: BLE001 — «message is not modified» и т.п.
            pass

    async def cleanup(self):
        """Удаляем файлы задач, у которых вышел срок (результат выдан / ссылка истекла)."""
        now = utcnow()
        for job in self.db.expired_cut_jobs(iso(now)):
            shutil.rmtree(job_dir(self.s, job["id"]), ignore_errors=True)
            self.db.update_cut_job(job["id"], delete_at=None, out_path=None, src_path=None, dl_token=None)
        # брошенные незавершённые загрузки — через сутки
        stale = self.db.q("SELECT id FROM cut_jobs WHERE status IN ('uploading', 'uploaded') AND created_at < ?",
                          iso(now - timedelta(days=1)))
        for row in stale:
            shutil.rmtree(job_dir(self.s, row["id"]), ignore_errors=True)
            self.db.update_cut_job(row["id"], status="failed", error="загрузка не завершена")

    def link_valid(self, job):
        return bool(job and job["dl_token"] and job["out_path"] and Path(job["out_path"]).exists()
                    and (not job["delete_at"] or from_iso(job["delete_at"]) > utcnow()))
