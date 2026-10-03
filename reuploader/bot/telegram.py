"""Минимальный клиент Telegram Bot API (long polling) на aiohttp."""
import asyncio
import html
import logging

import aiohttp
from pathlib import Path

log = logging.getLogger("tg")


def esc(s):
    return html.escape(str(s or ""), quote=False)


class TG:
    def __init__(self, token, session: aiohttp.ClientSession):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.session = session

    async def call(self, method, **params):
        params = {k: v for k, v in params.items() if v is not None}
        async with self.session.post(self.base + method, json=params,
                                     timeout=aiohttp.ClientTimeout(total=70)) as r:
            data = await r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data["result"]

    async def send(self, chat_id, text, buttons=None):
        """buttons: список рядов, ряд — список dict-кнопок Telegram."""
        markup = {"inline_keyboard": buttons} if buttons else None
        try:
            return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                                   reply_markup=markup, link_preview_options={"is_disabled": True})
        except Exception as e:  # noqa: BLE001 — уведомление не должно ронять бота
            log.warning("не удалось отправить сообщение: %s", e)

    async def send_video(self, chat_id, path, caption, width=None, height=None):
        """Видео в чат. Размер, длительность и картинка-превью берутся из файла: без них Telegram
        показывает чёрный квадрат вместо кадра, пока видео не скачаешь."""
        import asyncio

        meta = await asyncio.get_running_loop().run_in_executor(None, video_meta, path)
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption[:1024])
        form.add_field("parse_mode", "HTML")
        form.add_field("supports_streaming", "true")
        w, h = width or meta.get("width"), height or meta.get("height")
        if w and h:
            form.add_field("width", str(w))
            form.add_field("height", str(h))
        if meta.get("duration"):
            form.add_field("duration", str(int(round(meta["duration"]))))
        thumb = meta.get("thumb")
        try:
            with open(path, "rb") as f:
                form.add_field("video", f, filename="short.mp4", content_type="video/mp4")
                if thumb:
                    form.add_field("thumbnail", Path(thumb).read_bytes(), filename="thumb.jpg",
                                   content_type="image/jpeg")
                async with self.session.post(self.base + "sendVideo", data=form,
                                             timeout=aiohttp.ClientTimeout(total=1800)) as r:
                    data = await r.json()
        finally:
            if thumb:
                Path(thumb).unlink(missing_ok=True)
        if not data.get("ok"):
            raise RuntimeError(f"Telegram sendVideo: {data.get('description')}")
        return data["result"]

    async def send_document(self, chat_id, path, caption, filename="cover.jpg", content_type="image/jpeg"):
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption[:1024])
        with open(path, "rb") as f:
            form.add_field("document", f, filename=filename, content_type=content_type)
            async with self.session.post(self.base + "sendDocument", data=form,
                                         timeout=aiohttp.ClientTimeout(total=90)) as r:
                data = await r.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram: не удалось отправить файл")
        return data["result"]

    async def download(self, file_id, dst):
        """Скачивает файл, присланный боту (голосовое, аудио). Лимит Telegram — 20 МБ."""
        info = await self.call("getFile", file_id=file_id)
        url = self.base.replace("/bot", "/file/bot", 1) + info["file_path"]
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as r:
            if r.status != 200:
                raise RuntimeError(f"Telegram: не удалось скачать файл ({r.status})")
            with open(dst, "wb") as f:
                async for part in r.content.iter_chunked(256 * 1024):
                    f.write(part)
        return dst

    async def poll(self, handler):
        offset = None
        while True:
            try:
                updates = await self.call("getUpdates", offset=offset, timeout=50,
                                          allowed_updates=["message", "callback_query", "my_chat_member"])
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("getUpdates: %s — повтор через 5 с", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    await handler(u)
                except Exception:  # noqa: BLE001
                    log.exception("ошибка обработки апдейта")


def video_meta(path):
    """Ширина, высота, длительность и превью-кадр (JPEG до 320 px, как требует Telegram)."""
    import subprocess

    from ..ffmpeg_path import ffmpeg_exe
    from ..smartcut.media import probe

    out = {}
    try:
        info = probe(path)
        out.update(width=info.width, height=info.height, duration=info.duration)
        thumb = Path(str(path) + ".thumb.jpg")
        at = min(1.0, max(0.0, (info.duration or 0) / 3))
        subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(path), "-frames:v", "1",
                        "-vf", "scale='if(gt(iw,ih),320,-2)':'if(gt(iw,ih),-2,320)'", "-q:v", "4", str(thumb)],
                       check=True, timeout=60)
        if thumb.exists() and thumb.stat().st_size < 200 * 1024:
            out["thumb"] = str(thumb)
    except Exception:  # noqa: BLE001 — без превью видео всё равно уйдёт
        pass
    return out
