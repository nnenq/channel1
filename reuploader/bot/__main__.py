"""Запуск: python -m reuploader.bot"""
import asyncio
import logging
import os
import secrets
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from aiohttp import web

from . import fmt
from .cutjobs import CutWorker, job_dir
from .db import DB, iso, utcnow
from .tunnel import ensure_tunnel, stop_saved_tunnel
from .scheduler import Scheduler, describe_slot
from .settings import load_settings
from .telegram import TG, esc
from .web import WebApp

log = logging.getLogger("bot")
INVITE_TTL = timedelta(hours=24)
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
        self.username = ""

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

    def has_access(self, uid):
        return uid is not None and (uid == self.owner_id or uid in self.db.allowed_ids())

    def recipients(self):
        """Кому слать уведомления: владелец и все, кому он дал доступ."""
        ids = [self.owner_id] if self.owner_id else []
        return ids + [u for u in self.db.allowed_ids() if u != self.owner_id]

    def project_user(self, project):
        return project.get("user_id") or self.owner_id

    async def notify_text(self, text, buttons=None, project=None):
        """Уведомление хозяину проекта (без проекта — владельцу бота)."""
        uid = self.project_user(project) if project else self.owner_id
        if uid:
            await self.tg.send(uid, text, buttons)

    # ----- доступ -----
    async def grant(self, uid, name=None, username=None, via=""):
        self.db.set_user(uid, "allowed", name, username)
        await self.set_menu_button(uid)
        btn = self.app_button()
        await self.tg.send(uid, "✅ Владелец дал тебе доступ к боту. Панель — кнопкой ниже или «Панель» "
                                "слева от поля ввода.\n/status — план на сегодня", [[btn]] if btn else None)
        who = esc(name or username or uid)
        await self.tg.send(self.owner_id, f"👤 {who} теперь имеет доступ{via}.")

    async def revoke(self, uid, block=False):
        self.db.pause_user_projects(uid)
        if block:
            self.db.set_user(uid, "blocked")
        else:
            self.db.delete_user(uid)
        try:
            await self.tg.call("setChatMenuButton", chat_id=uid, menu_button={"type": "default"})
        except Exception:  # noqa: BLE001
            pass

    def create_invite(self):
        code = secrets.token_urlsafe(9)
        self.db.create_invite(code)
        return f"https://t.me/{self.username}?start=inv_{code}"

    async def request_access(self, user):
        """Незнакомец написал /start — спрашиваем владельца."""
        name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or None
        uname = user.get("username")
        self.db.set_user(user["id"], "pending", name, uname)
        await self.tg.send(user["id"], "Это личный бот. Запрос на доступ отправлен владельцу — "
                                       "если он одобрит, я напишу.")
        who = esc(name or "без имени") + (f" (@{esc(uname)})" if uname else "")
        await self.tg.send(self.owner_id, f"👤 {who}, id <code>{user['id']}</code>, просит доступ к боту.", [[
            {"text": "✅ Дать доступ", "callback_data": f"acc:{user['id']}"},
            {"text": "❌ Отклонить", "callback_data": f"dec:{user['id']}"},
        ]])

    # ----- уведомления от планировщика -----
    async def uploaded(self, project, result):
        await self.notify_text(
            f"✅ <b>{esc(project['channel_title'] or project['name'])}</b>: "
            + (f"запланировано на YouTube — выйдет в {result.extra['publish_at'].astimezone(self.s.tz):%H:%M}"
               if result.extra.get("publish_at")
               else f"залито ({PRIVACY_RU.get(project['privacy'], project['privacy'])})") + "\n"
            f"{esc(result.title)}\nОригинал: {fmt.original_line(result.extra, self.s.tz)}"
            f"{esc(fmt.fit_line(result.extra))}{esc(fmt.skipped_sources_line(result.extra))}"
            + (f"\n🕰 {esc(result.extra['pick_note'])}" if result.extra.get("pick_note") else "")
            + f"\n{result.info}"
            + ("\n⚠️ " + esc(result.extra["warning"]) if result.extra.get("warning") else ""), project=project)

    async def autodeleted(self, project, deleted, error=None):
        name = esc(project["channel_title"] or project["name"])
        lines = [f"🗑 <b>{name}</b>: удалил ролики без просмотров за {project['autodelete_hours']} ч:"]
        lines += [f"• {esc(t)}" for t in deleted[:20]]
        if not deleted:
            lines = [f"⚠️ <b>{name}</b>: автоудаление роликов с 0 просмотров"]
        if error:
            lines.append(f"⚠️ {esc(error)}")
        await self.notify_text("\n".join(lines), project=project)

    async def cover(self, project, result):
        caption = "Обложка: " + result.title + "\n"
        if result.extra.get("cover_status") == "set":
            caption += "YouTube принял обложку. Отображение в Shorts зависит от площадки."
        else:
            caption += "Готовый JPG для ручной установки в YouTube Studio."
        await self.tg.send_document(self.project_user(project), result.extra["cover"], caption)

    async def video(self, project, result):
        """Обработанное видео — всем, у кого есть доступ, с текстом для ручной публикации."""
        e = result.extra
        caption = (f"🎬 <b>{esc(project['name'])}</b>: готово к публикации (уже обработано)\n"
                   f"{esc(result.title)}\nОригинал: {fmt.original_line(e, self.s.tz)}{esc(fmt.fit_line(e))}\n"
                   f"{e['source_url']}")
        try:
            await self.tg.send_video(self.project_user(project), e["file"], caption)
        except Exception as err:  # noqa: BLE001
            await self.notify_text(f"❌ <b>{esc(project['name'])}</b>: не смог отправить видео в Telegram: "
                                   f"<code>{esc(err)}</code>", project=project)
            return
        tags = " ".join("#" + t.replace(" ", "") for t in e.get("tags") or [])
        await self.notify_text(
            "📋 Текст для публикации (нажми, чтобы скопировать):\n\n"
            f"<b>Название:</b>\n<code>{esc(result.title)}</code>\n\n"
            + (f"<b>Описание:</b>\n<code>{esc(e.get('description'))[:3000]}</code>\n\n" if e.get("description") else "")
            + (f"<b>Теги:</b>\n<code>{esc(tags)}</code>" if tags else ""), project=project)

    async def failed(self, project, result, retry_at):
        tail = f"\nПопробую ещё раз в {retry_at:%H:%M}." if retry_at else ""
        if result.auth_problem:
            tail = "\nОткрой панель → проект → «Привязать канал»."
        what = f" «{esc(result.title)}»" if result.title else ""
        btn = self.app_button("⚙️ Открыть проект", f"#p{project['id']}")
        await self.notify_text(
            f"❌ <b>{esc(project['name'])}</b>: не получилось залить{what}\n"
            f"<code>{esc(result.info)[:700]}</code>{tail}", [[btn]] if btn else None, project=project)

    async def skipped(self, project, slot):
        await self.notify_text(
            f"⏭ <b>{esc(project['name'])}</b>: пропущена публикация на "
            f"{describe_slot(slot, self.s.tz, datetime.now(self.s.tz).date())} — бот был выключен.", project=project)

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
               f"его можно увеличить в панели." if project["max_age_days"] and not project.get("fallback_old") else "")
            + (f"\n\nСтоит тема «{esc(project['topic'])}» — бот берёт только ролики про неё. "
               f"Можно расширить тему (добавить варианты через запятую) или убрать её в панели."
               if project.get("topic") else "")
            + (f"\n\nСтоит порог «от {fmt.views(project['min_views'])} просмотров» — "
               f"ролики слабее бот не берёт; порог можно снизить в панели." if project.get("min_views") else ""),
            rows, project=project)

    # ----- входящие сообщения -----
    async def handle(self, update):
        if "callback_query" in update:
            return await self.on_callback(update["callback_query"])
        if "my_chat_member" in update:
            return await self.on_added(update["my_chat_member"])
        msg = update.get("message") or {}
        user = msg.get("from") or {}
        text = (msg.get("text") or "").strip()
        if user and msg.get("chat", {}).get("type") == "private" and self.has_access(user["id"]) \
                and (msg.get("video") or (msg.get("document") or {}).get("mime_type", "").startswith("video/")):
            return await self.on_video(msg)
        if not user or not text.startswith("/") or msg["chat"].get("type") != "private":
            return
        chat = msg["chat"]["id"]

        if not self.owner_id and text.startswith("/start"):
            self.db.set_meta("owner_id", user["id"])
            self.db.claim_orphan_projects(user["id"])
            log.info("владелец бота: %s (%s)", user.get("username"), user["id"])
            await self.set_menu_button(user["id"])
        if not self.has_access(user["id"]):
            known = self.db.user(user["id"])
            arg = text.split(maxsplit=1)[1] if " " in text else ""
            if arg.startswith("inv_") and not (known and known["status"] == "blocked"):
                if self.db.use_invite(arg[4:], user["id"], INVITE_TTL):
                    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or None
                    await self.grant(user["id"], name, user.get("username"), " по приглашению")
                else:
                    await self.tg.send(chat, "Приглашение уже использовано или устарело — попроси новое.")
                return
            if not known and text.startswith("/start"):
                await self.request_access(user)
            # pending — запрос уже отправлен, blocked — молчим
            return

        if text.startswith("/panel"):
            btn = self.app_button()
            await self.set_menu_button(user["id"])
            await self.tg.send(chat, "Актуальная кнопка панели:" if btn else "⚠️ Панель сейчас недоступна.",
                               [[btn]] if btn else None)
            return
        if text.startswith("/start") or text.startswith("/app"):
            btn = self.app_button()
            await self.tg.send(
                chat,
                "Привет! Я перезаливаю самые просматриваемые шортсы с твоих каналов на твой другой канал "
                "по расписанию.\n\nВсё настраивается в панели: проекты, каналы, время публикаций.\n"
                "/status — план на сегодня\n/panel — свежая кнопка панели, если старая не открывается",
                [[btn]] if btn else None)
            if not btn:
                await self.tg.send(chat, "⚠️ Панель пока недоступна: нет HTTPS-адреса (см. окно бота).")
        elif text.startswith("/status"):
            await self.tg.send(chat, self.status_text(user["id"]))

    async def on_video(self, msg):
        """Видео прямо в чат (до 20 МБ — лимит Bot API на скачивание ботом)."""
        uid = msg["from"]["id"]
        f = msg.get("video") or msg.get("document")
        size = f.get("file_size") or 0
        btn = self.app_button("✂️ Открыть «Умную обрезку»", "#cut")
        if size > 20 * 1024 * 1024:
            await self.tg.send(uid, "Файл больше 20 МБ — Telegram не даёт боту его скачать. "
                                    "Загрузи его через панель → «Умная обрезка» (там лимит "
                                    f"{self.s.cut_max_mb} МБ).", [[btn]] if btn else None)
            return
        name = f.get("file_name") or "video.mp4"
        info = await self.tg.call("getFile", file_id=f["file_id"])
        jid = self.db.create_cut_job(uid, name, size, status="uploading")
        d = job_dir(self.s, jid)
        d.mkdir(parents=True, exist_ok=True)
        src = d / ("src" + (Path(name).suffix.lower() or ".mp4"))
        url = self.tg.base.replace("/bot", "/file/bot", 1) + info["file_path"]
        async with self.tg.session.get(url) as r:
            src.write_bytes(await r.read())
        self.db.update_cut_job(jid, src_path=str(src), status="uploaded", size=src.stat().st_size)
        btn = self.app_button("✂️ Выбрать длину и обрезать", f"#cut{jid}")
        await self.tg.send(uid, f"Видео «{esc(name)}» получил. Выбери целевую длину в панели:",
                           [[btn]] if btn else None)

    def status_text(self, uid):
        today = datetime.now(self.s.tz).date()
        icons = {"planned": "🕒", "running": "⏳", "done": "✅", "failed": "❌", "skipped": "⏭"}
        lines = []
        for p in self.db.projects(uid):
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
        if data.startswith(("ai_yes:", "ai_no:")):
            job = self.db.cut_job(int(data.split(":")[1]))
            if not job or job["user_id"] != user.get("id"):
                answer = "Нет доступа"
            else:
                ok, answer = self.cut.decide_ai(job, data.startswith("ai_yes:"))
                if ok and cq.get("message"):
                    m = cq["message"]
                    try:
                        await self.tg.call("editMessageReplyMarkup", chat_id=m["chat"]["id"],
                                           message_id=m["message_id"], reply_markup={"inline_keyboard": []})
                    except Exception:  # noqa: BLE001
                        pass
            await self.tg.call("answerCallbackQuery", callback_query_id=cq["id"], text=answer)
            return
        if data.startswith(("acc:", "dec:")) and user.get("id") == self.owner_id:
            uid = int(data[4:])
            known = self.db.user(uid) or {}
            if data.startswith("acc:"):
                await self.grant(uid, known.get("name"), known.get("username"))
                answer = "Доступ выдан"
            else:
                await self.revoke(uid, block=True)
                answer = "Отклонено"
            if cq.get("message"):
                m = cq["message"]
                try:
                    await self.tg.call("editMessageText", chat_id=m["chat"]["id"], message_id=m["message_id"],
                                       text=m.get("text", "") + ("\n\n✅ Доступ выдан" if data.startswith("acc:")
                                                                 else "\n\n❌ Отклонено"))
                except Exception:  # noqa: BLE001
                    pass
            await self.tg.call("answerCallbackQuery", callback_query_id=cq["id"], text=answer)
            return
        if not self.has_access(user.get("id")):
            answer = "Нет доступа"
        elif data.startswith("rp:"):
            _, pid, vid = data.split(":", 2)
            p = self.db.project(int(pid))
            if p and self.project_user(p) == user.get("id"):
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

    async def set_menu_button(self, uid=None):
        """Кнопка «Панель» у владельца и всех, кому дан доступ (или у одного uid)."""
        if not self.public_url:
            return
        for chat in ([uid] if uid else self.recipients()):
            try:
                await self.tg.call("setChatMenuButton", chat_id=chat, menu_button={
                    "type": "web_app", "text": "Панель", "web_app": {"url": self.app_url()}})
            except Exception as e:  # noqa: BLE001
                log.warning("не удалось поставить кнопку меню для %s: %s", chat, e)


def find_cloudflared(configured):
    for c in (configured, shutil.which("cloudflared"), "bin/cloudflared.exe", "bin/cloudflared"):
        if c and Path(c).exists() or (c and shutil.which(c)):
            return c
    return None


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
        bot.username = me["username"]
        if bot.owner_id:
            db.claim_orphan_projects(bot.owner_id)
        sched = Scheduler(db, s, bot)
        bot.sched = sched
        bot.cut = CutWorker(db, s, bot)

        runner = web.AppRunner(WebApp(db, s, sched, bot).build(), access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", s.port).start()
        except OSError as e:
            await runner.cleanup()
            raise AlreadyRunning(s.port) from e
        log.info("веб-сервер: %s", s.local_url)

        url_changed = False
        if s.public_url:
            bot.public_url = s.public_url
            stop_saved_tunnel(s.data_dir)
            url_changed = db.get_meta("last_public_url") not in (None, s.public_url)
            db.set_meta("last_public_url", s.public_url)
        else:
            exe = find_cloudflared(s.cloudflared)
            if exe:
                url, reused = await ensure_tunnel(exe, s.local_url, s.data_dir)
                bot.public_url = url or ""
                url_changed = bool(url) and not reused and db.get_meta("last_public_url") not in (None, url)
                if url:
                    db.set_meta("last_public_url", url)
            if not bot.public_url:
                log.warning("нет HTTPS-адреса: мини-апка не откроется (нужен cloudflared или PUBLIC_URL)")
        if bot.public_url:
            log.info("мини-апка: %s/app", bot.public_url)
        await bot.set_menu_button()

        keep_awake()
        print(f"\n  Бот @{me['username']} запущен. Напиши ему /start в Telegram.\n"
              f"  Не закрывай это окно, пока нужны заливки.\n", flush=True)
        if bot.owner_id:
            note = ("\n\n🔄 Адрес панели обновился — старые кнопки «Панель» в чате больше не открываются, "
                    "жми эту или /panel.") if url_changed else ""
            await bot.tg.send(bot.owner_id, "🟢 Бот запущен." + note + "\n" + bot.status_text(bot.owner_id),
                              [[bot.app_button()]] if bot.app_button() else None)
        try:
            await asyncio.gather(tg.poll(bot.handle), sched.loop(), bot.cut.loop())
        finally:
            await runner.cleanup()


class AlreadyRunning(Exception):
    pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except AlreadyRunning as e:
        print(f"\n  ⚠️ Порт {e.args[0]} занят — скорее всего бот УЖЕ запущен в другом окне\n"
              f"  (или свёрнут после автозапуска). Оставь одно окно бота, это можно закрыть.\n"
              f"  Если других окон нет: Диспетчер задач -> процессы Python -> Снять задачу.\n", flush=True)
        sys.exit(3)
