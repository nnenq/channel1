"""Запуск: python -m reuploader.bot"""
import asyncio
import logging
import os
import re
import secrets
import shutil
import sys
from datetime import datetime
from pathlib import Path

import aiohttp
from aiohttp import web

from . import fmt
from .db import DB, iso, utcnow
from .scheduler import Scheduler, describe_slot
from .settings import load_settings
from .telegram import TG, esc
from .web import WebApp

log = logging.getLogger("bot")
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
PRIVACY_RU = {"public": "публичное", "unlisted": "по ссылке", "private": "приватное",
              "scheduled": "публичное"}


class BotApp:
    """Telegram-часть: команды, кнопки и уведомления."""

    def __init__(self, db, settings, tg):
        self.db = db
        self.s = settings
        self.tg = tg
        self.public_url = ""
        self.sched = None

    @property
    def owner_id(self):
        if self.s.owner_id:
            return self.s.owner_id
        v = self.db.get_meta("owner_id")
        return int(v) if v else None

    @property
    def app_key(self):
        """Секрет в адресе панели: без него страница отвечает «не найдено»."""
        key = self.db.get_meta("app_key")
        if not key:
            key = secrets.token_urlsafe(18)
            self.db.set_meta("app_key", key)
        return key

    def app_url(self, fragment=""):
        return f"{self.public_url}/app?k={self.app_key}{fragment}" if self.public_url else None

    def app_button(self, text="📱 Открыть панель", fragment=""):
        url = self.app_url(fragment)
        return {"text": text, "web_app": {"url": url}} if url else None

    async def notify_text(self, text, buttons=None):
        if self.owner_id:
            await self.tg.send(self.owner_id, text, buttons)

    # ----- уведомления от планировщика -----
    async def uploaded(self, project, result):
        await self.notify_text(
            f"✅ <b>{esc(project['channel_title'] or project['name'])}</b>: "
            + (f"запланировано на YouTube — выйдет в {result.extra['publish_at'].astimezone(self.s.tz):%H:%M}"
               if result.extra.get("publish_at")
               else f"залито ({PRIVACY_RU.get(project['privacy'], project['privacy'])})") + "\n"
            f"{esc(result.title)}\nОригинал: {fmt.original_line(result.extra, self.s.tz)}\n{result.info}")

    async def video(self, project, result):
        """Обработанное видео — владельцу в Telegram, с текстом для ручной публикации."""
        e = result.extra
        caption = (f"🎬 <b>{esc(project['name'])}</b>: готово к публикации (уже обработано)\n"
                   f"{esc(result.title)}\nОригинал: {fmt.original_line(e, self.s.tz)}\n{e['source_url']}")
        try:
            await self.tg.send_video(self.owner_id, e["file"], caption)
        except Exception as err:  # noqa: BLE001
            await self.notify_text(f"❌ <b>{esc(project['name'])}</b>: не смог отправить видео в Telegram: "
                                   f"<code>{esc(err)}</code>")
            return
        tags = " ".join("#" + t.replace(" ", "") for t in e.get("tags") or [])
        await self.notify_text(
            "📋 Текст для публикации (нажми, чтобы скопировать):\n\n"
            f"<b>Название:</b>\n<code>{esc(result.title)}</code>\n\n"
            + (f"<b>Описание:</b>\n<code>{esc(e.get('description'))[:3000]}</code>\n\n" if e.get("description") else "")
            + (f"<b>Теги:</b>\n<code>{esc(tags)}</code>" if tags else ""))

    async def failed(self, project, result, retry_at):
        tail = f"\nПопробую ещё раз в {retry_at:%H:%M}." if retry_at else ""
        if result.auth_problem:
            tail = "\nОткрой панель → проект → «Привязать канал»."
        what = f" «{esc(result.title)}»" if result.title else ""
        btn = self.app_button("⚙️ Открыть проект", f"#p{project['id']}")
        await self.notify_text(
            f"❌ <b>{esc(project['name'])}</b>: не получилось залить{what}\n"
            f"<code>{esc(result.info)[:700]}</code>{tail}", [[btn]] if btn else None)

    async def skipped(self, project, slot):
        await self.notify_text(
            f"⏭ <b>{esc(project['name'])}</b>: пропущена публикация на "
            f"{describe_slot(slot, self.s.tz, datetime.now(self.s.tz).date())} — бот был выключен.")

    async def exhausted(self, project):
        rows = []
        for c in self.db.repost_candidates(project["id"], 3):
            views = f" · {fmt.views(c['views'])}" if c["views"] else ""
            rows.append([{"text": f"🔁 {(c['title'] or c['video_id'])[:40]}{views}",
                          "callback_data": f"rp:{project['id']}:{c['video_id']}"}])
        btn = self.app_button("➕ Сменить/добавить каналы", f"#p{project['id']}")
        if btn:
            rows.append([btn])
        rows.append([{"text": "⏳ Ждать новые видео", "callback_data": f"wait:{project['id']}"}])
        await self.notify_text(
            f"📭 <b>{esc(project['name'])}</b>: на каналах-источниках закончились новые видео — "
            f"всё уже перезалито.\n\nЧто делаем?\n"
            f"• 🔁 перезалить один из самых популярных роликов ещё раз;\n"
            f"• ➕ добавить или заменить каналы-источники;\n"
            f"• ⏳ ждать — как только на каналах появятся новые шортсы, продолжу сам."
            + (f"\n\nСейчас стоит фильтр «не старше {project['max_age_days']} дн.» — "
               f"его можно увеличить в панели." if project["max_age_days"] else ""), rows)

    # ----- входящие сообщения -----
    async def handle(self, update):
        if "callback_query" in update:
            return await self.on_callback(update["callback_query"])
        if "my_chat_member" in update:
            return await self.on_added(update["my_chat_member"])
        msg = update.get("message") or {}
        user = msg.get("from") or {}
        text = (msg.get("text") or "").strip()
        if not user or not text.startswith("/") or msg["chat"].get("type") != "private":
            return
        chat = msg["chat"]["id"]

        if not self.owner_id and text.startswith("/start"):
            self.db.set_meta("owner_id", user["id"])
            log.info("владелец бота: %s (%s)", user.get("username"), user["id"])
            await self.set_menu_button()
        if user["id"] != self.owner_id:
            # Чужим не отвечаем вообще — для них бот выглядит неработающим
            log.info("чужой пользователь %s (id %s) написал боту — игнорирую",
                     user.get("username"), user["id"])
            return

        if text.startswith("/start") or text.startswith("/app"):
            btn = self.app_button()
            await self.tg.send(
                chat,
                "Привет! Я перезаливаю самые просматриваемые шортсы с твоих каналов на твой другой канал "
                "по расписанию.\n\nВсё настраивается в панели: проекты, каналы, время публикаций.\n"
                "/status — план на сегодня",
                [[btn]] if btn else None)
            if not btn:
                await self.tg.send(chat, "⚠️ Панель пока недоступна: нет HTTPS-адреса (см. окно бота).")
        elif text.startswith("/status"):
            await self.tg.send(chat, self.status_text())

    def status_text(self):
        today = datetime.now(self.s.tz).date()
        icons = {"planned": "🕒", "running": "⏳", "done": "✅", "failed": "❌", "skipped": "⏭"}
        lines = []
        for p in self.db.projects():
            head = f"<b>{esc(p['name'])}</b> → {esc(p['channel_title'] or 'канал не привязан')}"
            if not p["enabled"]:
                head += " (пауза)"
            lines.append(head)
            slots = [s for s in self.db.slots_for_date(p["id"], today.isoformat()) if s["status"] != "cancelled"]
            for s in slots:
                lines.append(f"  {icons.get(s['status'], '')} {describe_slot(s, self.s.tz, today)}")
            if not slots:
                lines.append("  на сегодня ничего")
        return "\n".join(lines) or "Проектов пока нет — открой панель и создай первый."

    async def on_added(self, upd):
        """Бота добавили в группу/канал — сразу выходим."""
        chat = upd.get("chat", {})
        if chat.get("type") != "private" and upd.get("new_chat_member", {}).get("status") in ("member", "administrator"):
            log.info("бота добавили в %s «%s» — выхожу", chat.get("type"), chat.get("title"))
            try:
                await self.tg.call("leaveChat", chat_id=chat["id"])
            except Exception:  # noqa: BLE001
                pass

    async def on_callback(self, cq):
        data = cq.get("data", "")
        user = cq.get("from", {})
        answer = "Готово"
        if user.get("id") != self.owner_id:
            answer = "Нет доступа"
        elif data.startswith("rp:"):
            _, pid, vid = data.split(":", 2)
            p = self.db.project(int(pid))
            if p:
                now = datetime.now(self.s.tz)
                self.db.add_slot(p["id"], now.date().isoformat(), iso(utcnow()), "manual",
                                 f"https://www.youtube.com/shorts/{vid}")
                self.sched.poke()
                answer = "Заливаю этот ролик повторно"
        elif data.startswith("wait:"):
            answer = "Ок, жду новые видео"
        await self.tg.call("answerCallbackQuery", callback_query_id=cq["id"], text=answer)
        if data.startswith(("rp:", "wait:")) and cq.get("message"):
            m = cq["message"]
            try:
                await self.tg.call("editMessageReplyMarkup", chat_id=m["chat"]["id"],
                                   message_id=m["message_id"], reply_markup={"inline_keyboard": []})
                await self.tg.send(m["chat"]["id"], answer + ".")
            except Exception:  # noqa: BLE001
                pass

    async def set_menu_button(self):
        if not (self.owner_id and self.public_url):
            return
        try:
            await self.tg.call("setChatMenuButton", chat_id=self.owner_id, menu_button={
                "type": "web_app", "text": "Панель", "web_app": {"url": self.app_url()}})
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось поставить кнопку меню: %s", e)


def find_cloudflared(configured):
    for c in (configured, shutil.which("cloudflared"), "bin/cloudflared.exe", "bin/cloudflared"):
        if c and Path(c).exists() or (c and shutil.which(c)):
            return c
    return None


async def start_tunnel(exe, local_url):
    """Бесплатный HTTPS-адрес через Cloudflare Quick Tunnel (меняется при каждом запуске)."""
    proc = await asyncio.create_subprocess_exec(
        exe, "tunnel", "--no-autoupdate", "--url", local_url,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    url = None
    deadline = asyncio.get_running_loop().time() + 60
    while url is None:
        timeout = deadline - asyncio.get_running_loop().time()
        if timeout <= 0:
            break
        try:
            line = await asyncio.wait_for(proc.stderr.readline(), timeout)
        except asyncio.TimeoutError:
            break
        if not line:
            break
        m = TUNNEL_RE.search(line.decode(errors="ignore"))
        if m:
            url = m.group(0)

    async def drain():
        while await proc.stderr.readline():
            pass

    asyncio.create_task(drain())
    return proc, url


def keep_awake():
    """Windows: не давать компьютеру уснуть, пока бот запущен."""
    if sys.platform == "win32":
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log.info("спящий режим отключён, пока бот работает")


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("aiohttp.access", "googleapiclient.discovery_cache"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    s = load_settings()
    db = DB(s.db_path)
    async with aiohttp.ClientSession() as session:
        tg = TG(s.bot_token, session)
        me = await tg.call("getMe")
        bot = BotApp(db, s, tg)
        sched = Scheduler(db, s, bot)
        bot.sched = sched

        runner = web.AppRunner(WebApp(db, s, sched, bot).build(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", s.port).start()
        log.info("веб-сервер: %s", s.local_url)

        tunnel = None
        if s.public_url:
            bot.public_url = s.public_url
        else:
            exe = find_cloudflared(s.cloudflared)
            if exe:
                tunnel, url = await start_tunnel(exe, s.local_url)
                bot.public_url = url or ""
            if not bot.public_url:
                log.warning("нет HTTPS-адреса: мини-апка не откроется (нужен cloudflared или PUBLIC_URL)")
        if bot.public_url:
            log.info("мини-апка: %s/app", bot.public_url)
        await bot.set_menu_button()

        keep_awake()
        print(f"\n  Бот @{me['username']} запущен. Напиши ему /start в Telegram.\n"
              f"  Не закрывай это окно, пока нужны заливки.\n", flush=True)
        if bot.owner_id:
            await bot.notify_text("🟢 Бот запущен.\n" + bot.status_text(),
                                  [[bot.app_button()]] if bot.app_button() else None)
        try:
            await asyncio.gather(tg.poll(bot.handle), sched.loop())
        finally:
            if tunnel and tunnel.returncode is None:
                tunnel.terminate()
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
