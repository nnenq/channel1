"""Минимальный клиент Telegram Bot API (long polling) на aiohttp."""
import asyncio
import html
import logging

import aiohttp

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

    async def poll(self, handler):
        offset = None
        while True:
            try:
                updates = await self.call("getUpdates", offset=offset, timeout=50,
                                          allowed_updates=["message", "callback_query"])
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
