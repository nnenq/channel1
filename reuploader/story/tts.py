"""Бесплатная озвучка текста голосом Google (Gemini TTS из Google AI Studio).

Ключ бесплатный: https://aistudio.google.com/apikey -> в .env строка GEMINI_API_KEY=...
Бесплатный тариф есть у моделей *-tts (с ограничением числа запросов в минуту/день).
Запрос — POST /v1beta/interactions; ответ — base64-аудио в steps[].content[] (type=audio),
WAV (моно, 16 бит, 24 кГц). Длинный текст режется на куски по предложениям и склеивается.
"""
import base64
import io
import json
import logging
import re
import time
import urllib.error
import urllib.request
import wave

log = logging.getLogger("story.tts")
URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_MODEL = "gemini-3.8-flash-tts"
DEFAULT_VOICE = "Puck"
# Характер голосов — по описаниям Google (ai.google.dev/gemini-api/docs/speech-generation)
VOICE_INFO = {
    "Puck": "бодрый", "Fenrir": "возбуждённый, эмоциональный", "Sadachbia": "живой", "Laomedeia": "бодрый",
    "Charon": "информативный, как диктор", "Sadaltager": "знающий", "Rasalgethi": "информативный",
    "Enceladus": "с придыханием, таинственный", "Zephyr": "яркий", "Autonoe": "яркий", "Leda": "молодой",
    "Aoede": "лёгкий", "Callirrhoe": "непринуждённый", "Umbriel": "непринуждённый", "Zubenelgenubi": "разговорный",
    "Achird": "дружелюбный", "Sulafat": "тёплый", "Algieba": "плавный", "Despina": "плавный",
    "Iapetus": "чёткий", "Erinome": "чёткий", "Algenib": "с хрипотцой", "Gacrux": "зрелый",
    "Pulcherrima": "напористый", "Schedar": "ровный", "Kore": "строгий", "Orus": "строгий", "Alnilam": "строгий",
    "Achernar": "мягкий", "Vindemiatrix": "нежный",
}
VOICES = list(VOICE_INFO)
PREVIEW = {
    "ru": "Губка Боб даже не догадывался, что его язык — это Планктон. И вот что случилось дальше…",
    "en": "SpongeBob had no idea his tongue was actually Plankton. And here's what happened next…",
}
RATE = 24000
MAX_CHARS = 1200          # кусок текста на один запрос
STYLE = {
    "ru": "увлечённый рассказчик коротких видео: живо, энергично, с интригой, в среднем темпе",
    "en": "an engaging short-video storyteller: lively, energetic, suspenseful, medium pace",
}


class TTSError(Exception):
    pass


def chunks(text, limit=MAX_CHARS):
    sents = [s.strip() for s in re.split(r"(?<=[.!?…])\s+|\n+", text or "") if s.strip()]
    out, cur = [], ""
    for s in sents:
        if cur and len(cur) + 1 + len(s) > limit:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def _pcm_from_audio(raw):
    """WAV (с заголовком RIFF) или «голый» PCM -> кадры PCM 16 бит моно."""
    if raw[:4] == b"RIFF":
        with wave.open(io.BytesIO(raw)) as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise TTSError("неожиданный формат аудио от Google")
            return w.readframes(w.getnframes()), w.getframerate()
    return raw, RATE


def _request(text, voice, style, model, api_key, timeout=120):
    content = {"type": "text", "text": text}
    if style:
        content["annotations"] = [{"type": "speech_metadata", "style": style}]
    body = {"model": model, "input": [{"type": "user_input", "content": [content]}],
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": voice}]}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST", headers={
        "x-goog-api-key": api_key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    items = data if isinstance(data, list) else [data]       # ответ бывает и объектом, и списком частей
    audio = [c for d in items for st in (d.get("steps") or []) if st.get("type") == "model_output"
             for c in st.get("content", []) if c.get("type") == "audio" and c.get("data")]
    if not audio:
        raise TTSError("Google не вернул аудио (возможно, текст отклонён фильтром)")
    return base64.b64decode(audio[-1]["data"])


def synthesize(text, out_path, api_key, voice=DEFAULT_VOICE, lang="ru", model=DEFAULT_MODEL,
               retries=4, sleep=time.sleep, request=_request):
    """Озвучивает текст в WAV. Бесплатный тариф ограничен по частоте — на 429 ждём и повторяем."""
    if not api_key:
        raise TTSError("нет ключа: добавь GEMINI_API_KEY в .env (бесплатно на aistudio.google.com/apikey)")
    parts = chunks(text)
    if not parts:
        raise TTSError("пустой текст")
    pcm, rate = b"", RATE
    for part in parts:
        for attempt in range(retries + 1):
            try:
                frames, rate = _pcm_from_audio(request(part, voice, STYLE.get(lang, ""), model, api_key))
                break
            except urllib.error.HTTPError as e:
                detail = (e.read() or b"")[:600].decode("utf-8", "replace")
                if e.code == 429 and attempt < retries:        # лимит бесплатного тарифа — подождём
                    log.info("Google TTS: лимит запросов, жду…")
                    sleep(20 * (attempt + 1))
                    continue
                if e.code in (401, 403) or "API_KEY_INVALID" in detail:
                    raise TTSError("Google отклонил ключ GEMINI_API_KEY — проверь его в .env") from None
                if e.code == 429:
                    raise TTSError("исчерпан бесплатный лимит Google TTS на сегодня — попробуй позже "
                                   "или озвучь своим голосом") from None
                raise TTSError(f"Google TTS {e.code}: {detail}") from None
        pcm += frames + b"\x00\x00" * int(rate * 0.25)      # короткая пауза между кусками
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out_path
