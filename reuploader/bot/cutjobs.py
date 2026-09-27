"""Очередь «умной обрезки»: одна задача за раз, прогресс в Telegram, выдача результата.

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

from ..smartcut import CutError, format_report, smart_cut
from ..smartcut.analyze import whisper_transcribe
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


class CutWorker:
    def __init__(self, db, settings, bot):
        self.db = db
        self.s = settings
        self.bot = bot           # BotApp: tg, app_button, public_url
        self.wake = asyncio.Event()

    def poke(self):
        self.wake.set()

    async def loop(self):
        # задачи, прерванные перезапуском бота, — снова в очередь
        self.db.x("UPDATE cut_jobs SET status = 'queued', stage = 'в очереди' WHERE status = 'running'")
        while True:
            try:
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
        jid, uid = job["id"], job["user_id"]
        self.db.update_cut_job(jid, status="running", stage="старт", progress=0)
        msg = await self.bot.tg.send(uid, f"✂️ Обрезаю «{esc(job['filename'])}»…\n{bar(0)} 0%")
        if msg:
            self.db.update_cut_job(jid, tg_message_id=msg["message_id"])

        state = {"stage": "старт", "frac": 0.0}

        def progress(stage, frac):          # вызывается из рабочего потока
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
                                     f"✂️ Обрезаю «{esc(job['filename'])}»…\n{bar(state['frac'])} {cur[1]}% — {cur[0]}")

        out = job_dir(self.s, jid) / "out.mp4"
        tick = asyncio.create_task(ticker())
        try:
            report = await asyncio.get_running_loop().run_in_executor(None, partial(
                smart_cut, job["src_path"], out, job["target"], progress=progress,
                transcriber=partial(whisper_transcribe, model_size=self.s.whisper_model),
                work_dir=job_dir(self.s, jid) / "tmp"))
        except CutError as e:
            await self._fail(job, str(e), msg)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("обрезка %s", jid)
            await self._fail(job, f"{type(e).__name__}: {e}", msg)
            return
        finally:
            tick.cancel()
            shutil.rmtree(job_dir(self.s, jid) / "tmp", ignore_errors=True)
            Path(job["src_path"]).unlink(missing_ok=True)      # исходник больше не нужен

        text = format_report(report)
        if report["status"] == "already_short":
            self.db.update_cut_job(jid, status="done", stage="готово", progress=1, report=json.dumps(report),
                                   finished_at=iso(utcnow()), delete_at=iso(utcnow()))
            if msg:
                await self._edit(uid, msg["message_id"], "✂️ " + esc(text))
            return

        token = secrets.token_urlsafe(24)
        ttl = timedelta(hours=self.s.cut_link_ttl_h)
        self.db.update_cut_job(jid, status="done", stage="готово", progress=1, out_path=str(out),
                               report=json.dumps(report, ensure_ascii=False), dl_token=token,
                               finished_at=iso(utcnow()), delete_at=iso(utcnow() + ttl))
        if msg:
            await self._edit(uid, msg["message_id"], f"✅ Готово: «{esc(job['filename'])}»")
        size_mb = out.stat().st_size / 1024 / 1024
        link = f"{self.bot.public_url}/dl/{token}" if self.bot.public_url else None
        sent = False
        if size_mb <= TG_SEND_MAX_MB:
            try:
                await self.bot.tg.send_video(uid, str(out), f"✂️ {esc(job['filename'])}\n"
                                             f"{report['before']:.0f} с → {report['after']:.0f} с")
                sent = True
            except Exception as e:  # noqa: BLE001
                log.warning("не смог отправить видео: %s", e)
        tail = ""
        if not sent and link:
            tail = (f"\n\n📥 Файл {size_mb:.0f} МБ — больше лимита Telegram, скачай по ссылке "
                    f"(действует {self.s.cut_link_ttl_h} ч):\n{link}")
        await self.bot.tg.send(uid, esc(text)[:3900] + tail)
        if sent:        # отдали — удаляем результат (ссылка ещё 10 минут на всякий случай)
            self.db.update_cut_job(jid, delete_at=iso(utcnow() + timedelta(minutes=10)))

    async def _fail(self, job, error, msg):
        self.db.update_cut_job(job["id"], status="failed", error=error[:500], finished_at=iso(utcnow()),
                               delete_at=iso(utcnow()))
        text = f"❌ Не получилось обрезать «{esc(job['filename'])}»:\n<code>{esc(error)[:700]}</code>"
        if msg:
            await self._edit(job["user_id"], msg["message_id"], text)
        else:
            await self.bot.tg.send(job["user_id"], text)

    async def _edit(self, chat, message_id, text):
        try:
            await self.bot.tg.call("editMessageText", chat_id=chat, message_id=message_id,
                                   text=text, parse_mode="HTML")
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
