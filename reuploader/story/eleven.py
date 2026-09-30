"""Озвучка текста через ElevenLabs — у каждого пользователя свой API-ключ.

Ключ: elevenlabs.io -> профиль (слева внизу) -> API Keys -> Create API Key.
Нужны права Text to Speech и Voices (read). Ключ вводится в мини-апке («Пересказы и теории»),
хранится в базе бота и никогда не показывается целиком и не пишется в логи.
"""
import json
import logging
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

log = logging.getLogger("story.eleven")

API = "https://api.elevenlabs.io"
DEFAULT_MODEL = "eleven_multilingual_v2"       # понимает русский
RATE = 24000                                    # pcm_24000: 16 бит, моно
MAX_CHARS = 2500


class TTSError(Exception):
    pass


def mask(key):
    key = key or ""
    return (key[:3] + "…" + key[-4:]) if len(key) > 10 else "…"


def chunks(text, limit=MAX_CHARS):
    """Режет длинный текст по предложениям, чтобы каждый кусок был не длиннее limit."""
    import re

    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?…])\s+", " ".join(text.split())):
        while len(sent) > limit:
            parts += [cur] if cur else []
            cur = ""
            parts.append(sent[:limit])
            sent = sent[limit:]
        if cur and len(cur) + 1 + len(sent) > limit:
            parts.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    return parts + ([cur] if cur else [])


def _error(code, raw):
    try:
        detail = json.loads(raw).get("detail")
    except (ValueError, AttributeError):
        detail = None
    status = detail.get("status", "") if isinstance(detail, dict) else ""
    msg = (detail.get("message") if isinstance(detail, dict) else detail) or raw[:200]
    if status == "quota_exceeded":
        return TTSError("на аккаунте ElevenLabs закончились символы — подожди обновления лимита "
                        "или пополни тариф")
    if status == "detected_unusual_activity":
        return TTSError("ElevenLabs отключил бесплатный тариф для этого ключа (подозрение на VPN/несколько "
                        "аккаунтов) — нужен платный тариф или другой аккаунт")
    if status == "missing_permissions":
        return TTSError("у ключа не хватает прав: при создании ключа включи Text to Speech и Voices (read)")
    if code == 401 or status in ("invalid_api_key", "api_key_invalid") or "api key is invalid" in str(msg).lower():
        return TTSError("ElevenLabs отклонил ключ — проверь его в панели («Пересказы и теории»)")
    if status == "voice_not_found" or code == 404:
        return TTSError("голос не найден в твоём аккаунте ElevenLabs — выбери другой")
    return TTSError(f"ElevenLabs {code}: {msg}")


def _http(method, path, key, body=None, timeout=120):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"xi-api-key": key, "Content-Type": "application/json",
                                          "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise _error(e.code, e.read().decode("utf-8", "replace")) from None
    except urllib.error.URLError as e:
        raise TTSError(f"нет связи с ElevenLabs: {e.reason}") from None


def voices(key, request=_http):
    """Голоса, доступные этому ключу (стандартные + добавленные в «My Voices»)."""
    if not key:
        raise TTSError("нет ключа ElevenLabs")
    data = json.loads(request("GET", "/v2/voices?page_size=100", key))
    out = []
    for v in data.get("voices") or []:
        labels = v.get("labels") or {}
        info = ", ".join(x for x in (labels.get("gender"), labels.get("age"), labels.get("accent"),
                                     labels.get("description") or labels.get("descriptive"),
                                     labels.get("use_case")) if x)
        out.append({"id": v["voice_id"], "name": v.get("name") or v["voice_id"],
                    "info": info, "preview": v.get("preview_url") or ""})
    return out


def quota(key, request=_http):
    """-> (потрачено, лимит) символов в этом месяце или None, если у ключа нет прав смотреть."""
    try:
        d = json.loads(request("GET", "/v1/user/subscription", key))
        return int(d.get("character_count") or 0), int(d.get("character_limit") or 0)
    except (TTSError, ValueError):
        return None


def synthesize(text, out_path, key, voice_id, model=DEFAULT_MODEL, retries=3,
               sleep=time.sleep, request=_http):
    """Озвучивает текст голосом voice_id и пишет WAV (24 кГц, моно). -> путь."""
    if not key:
        raise TTSError("нет ключа ElevenLabs — впиши свой в панели («Пересказы и теории»)")
    if not voice_id:
        raise TTSError("не выбран голос ElevenLabs")
    parts = chunks(text)
    if not parts:
        raise TTSError("пустой текст")
    pcm = b""
    for i, part in enumerate(parts):
        body = {"text": part, "model_id": model}
        if i:
            body["previous_text"] = parts[i - 1][-500:]
        if i + 1 < len(parts):
            body["next_text"] = parts[i + 1][:500]
        for attempt in range(retries + 1):
            try:
                pcm += request("POST", f"/v1/text-to-speech/{voice_id}?output_format=pcm_{RATE}", key, body)
                break
            except TTSError as e:
                busy = "429" in str(e) or "too_many" in str(e)
                if not busy or attempt == retries:
                    raise
                log.info("ElevenLabs занят, жду…")
                sleep(5 * (attempt + 1))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm[: len(pcm) // 2 * 2])
    return out_path
