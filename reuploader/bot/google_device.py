"""Привязка YouTube-канала по коду (OAuth device flow): работает с любого устройства.

Нужен OAuth-клиент Google типа «TVs and Limited Input devices» (client_secret_tv.json).
Человек открывает google.com/device, вводит код и выбирает канал.
"""
import asyncio
import json
from pathlib import Path

DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# Для входа по коду Google разрешает только общий скоуп youtube (он включает загрузку видео)
SCOPE = "https://www.googleapis.com/auth/youtube"


def load_client(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    c = data.get("installed") or data.get("web") or data
    return c["client_id"], c["client_secret"]


async def start(session, client_id):
    async with session.post(DEVICE_URL, data={"client_id": client_id, "scope": SCOPE}) as r:
        data = await r.json()
    if "device_code" not in data:
        raise RuntimeError(data.get("error_description") or data.get("error") or str(data))
    return data   # device_code, user_code, verification_url, expires_in, interval


async def wait_token(session, client_id, client_secret, device_code, interval, expires_in):
    """Ждёт, пока человек введёт код. Возвращает token dict или бросает RuntimeError."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + expires_in
    while loop.time() < deadline:
        await asyncio.sleep(interval)
        async with session.post(TOKEN_URL, data={
            "client_id": client_id, "client_secret": client_secret, "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }) as r:
            data = await r.json()
        err = data.get("error")
        if not err:
            return data
        if err == "slow_down":
            interval += 5
        elif err != "authorization_pending":
            raise RuntimeError({"access_denied": "вход отменён",
                                "expired_token": "код устарел"}.get(err, err))
    raise RuntimeError("код устарел — попробуй ещё раз")


def token_json(token, client_id, client_secret):
    """Формат, который понимает google.oauth2.credentials.Credentials.from_authorized_user_file."""
    return json.dumps({
        "token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "token_uri": TOKEN_URL,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": token.get("scope", SCOPE).split(),
    })
