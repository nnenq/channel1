"""Сценарии пересказов и теорий по мультфильму.

Вход — расшифровка мультфильма (whisper, строки с таймкодами). Claude пишет N сценариев:
каждая фраза сценария привязана к отрезку мультфильма, который её иллюстрирует.
Автор канала озвучивает сценарий своим голосом — бот собирает ролик (story.assemble).

estimate(...)       — оценка цены без сети;
write_scripts(...)  — один запрос в Claude со строгой JSON-схемой (только после «Да» пользователя);
manual_script(...)  — свой текст без AI: фразы привязываются к сценам по совпадению слов.
"""
import json
import logging
import re

from ..smartcut import pricing as pr

log = logging.getLogger("story.script")
DEFAULT_MODEL = "claude-opus-5-5"
WORDS_PER_SEC = {"ru": 2.3, "en": 2.6}      # темп закадрового голоса
LANG_NAME = {"ru": "русском", "en": "английском"}
KIND_RU = {"recap": "пересказ", "theory": "теория"}

SYSTEM = """Ты — сценарист коротких вертикальных роликов (YouTube Shorts) о мультфильмах.
Тебе дают расшифровку мультфильма с таймкодами. Автор канала озвучит твой текст своим голосом \
поверх кадров из этого мультфильма (звук мультфильма будет выключен). Пиши так, чтобы это было \
интересно слушать и досматривать до конца.

Виды сценариев:
- recap (пересказ): сюжет эпизода/части фильма. Первая фраза — крючок (интрига, неожиданный факт, \
вопрос). Дальше — только ключевые события по порядку, поворот, финал или панчлайн. Мелочи выбрасывай.
- theory (теория): необычный взгляд на героя или сюжет, опирающийся на конкретные сцены из \
расшифровки как на «доказательства». Подавай как теорию («а что если…», «обратите внимание…»), \
не выдавай за официальный факт. Финал — вывод и вопрос зрителю, чтобы писали комментарии.

Правила:
- Живой разговорный язык, короткие фразы (6–16 слов), без канцелярита и без эмодзи в тексте.
- Не выдумывай событий, которых нет в расшифровке. Имена героев — как принято в этом языке.
- Каждая фраза — отдельный элемент lines с отрезком мультфильма [from, to] в секундах, который \
показывает то, о чём фраза (бери таймкоды из расшифровки; отрезок 2–10 с; внутри одного сценария \
не повторяй один и тот же отрезок).
- Несколько сценариев из одного мультфильма должны быть про разное: разные части сюжета, разные \
герои или разные теории — без повторов.
- title — название ролика для YouTube (до 90 символов, цепляющее, без кликбейта-обмана).
- overlay — короткая надпись сверху кадра на весь ролик (до 40 символов): главный вопрос или интрига.
- why — одно предложение по-русски: почему этот ролик должен зацепить зрителя."""


def schema():
    line = {"type": "object",
            "properties": {"text": {"type": "string"}, "from": {"type": "number"}, "to": {"type": "number"}},
            "required": ["text", "from", "to"], "additionalProperties": False}
    item = {"type": "object",
            "properties": {"title": {"type": "string"}, "kind": {"type": "string", "enum": ["recap", "theory"]},
                           "overlay": {"type": "string"}, "why": {"type": "string"},
                           "lines": {"type": "array", "items": line}},
            "required": ["title", "kind", "overlay", "why", "lines"], "additionalProperties": False}
    return {"type": "object", "properties": {"scripts": {"type": "array", "items": item}},
            "required": ["scripts"], "additionalProperties": False}


def lines_from_words(words, max_words=22, pause=0.7):
    """Слова whisper -> строки расшифровки [(start, end, text)]: по концу фразы или паузе."""
    out, cur = [], []
    for w in words:
        if cur and (w.start - cur[-1].end > pause or len(cur) >= max_words):
            out.append(cur)
            cur = []
        cur.append(w)
        if w.text.rstrip().endswith((".", "!", "?", "…")):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [(g[0].start, g[-1].end, " ".join(x.text.strip() for x in g).strip()) for g in out if g]


def transcript_text(lines):
    return "\n".join(f"[{s:.1f}–{e:.1f}] {t}" for s, e, t in lines)


def user_prompt(lines, kind, lang, count, seconds, duration, topic=""):
    words = int(seconds * WORDS_PER_SEC.get(lang, 2.4))
    what = {"recap": f"{count} пересказов", "theory": f"{count} теорий",
            "auto": f"{count} роликов — сам выбери для каждого, что зайдёт лучше: пересказ или теория"}[kind]
    focus = f"\nФокус: {topic}." if topic else ""
    return (f"Напиши {what} на {LANG_NAME.get(lang, lang)} языке. Длина каждого — около {seconds} секунд "
            f"озвучки (примерно {words} слов).{focus}\nДлина мультфильма: {duration:.0f} с.\n\n"
            f"Расшифровка:\n{transcript_text(lines)}")


def estimate(lines, count, seconds, lang="ru", model=DEFAULT_MODEL, pricing=None, kind="auto", duration=0):
    """Оценка токенов и цены без сети. -> dict(model, input_tokens, output_tokens, usd, rub)."""
    pricing = pricing or pr.load()
    cpt = float(pricing.get("estimate", {}).get("chars_per_token", 2.6))
    text = SYSTEM + json.dumps(schema(), ensure_ascii=False) + user_prompt(lines, kind, lang, count, seconds, duration)
    input_tokens = int(len(text) / cpt) + 200
    words = seconds * WORDS_PER_SEC.get(lang, 2.4)
    # текст сценария + таймкоды/JSON + запас на размышления модели
    output_tokens = int(count * (words * 2.2 + 400) + 3000)
    usd = pr.cost_usd(pricing, model, input_tokens, output_tokens)
    return {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "usd": round(usd, 4), "rub": round(usd * float(pricing.get("usd_rub", 90)), 2)}


def clean_scripts(items, duration):
    """Проверяет ответ модели: отрезки в пределах мультфильма, пустые фразы выброшены."""
    out = []
    for it in items:
        lines = []
        for ln in it.get("lines") or []:
            text = str(ln.get("text") or "").strip()
            if not text:
                continue
            a = max(0.0, min(float(ln.get("from") or 0), max(0.0, duration - 1)))
            b = max(a + 1.0, min(float(ln.get("to") or a + 4), duration or a + 4))
            lines.append({"text": text, "from": round(a, 2), "to": round(b, 2)})
        if lines:
            out.append({"title": str(it.get("title") or "")[:100].strip(),
                        "kind": it.get("kind") if it.get("kind") in KIND_RU else "recap",
                        "overlay": str(it.get("overlay") or "")[:60].strip(),
                        "why": str(it.get("why") or "").strip(), "lines": lines})
    return out


def write_scripts(client, lines, kind, lang, count, seconds, duration, topic="", model=DEFAULT_MODEL,
                  pricing=None, usage_log=None):
    """Один запрос в Claude. -> (scripts, note). usage_log(dict) — фактические токены и цена."""
    from ..smartcut.ai import usage_cost

    pricing = pricing or pr.load()
    # «default»-fallback: если Claude Opus 5.5 отклонит запрос классификатором безопасности,
    # сервер сам повторит его на другой модели внутри того же вызова.
    resp = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},
        system=SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema()}},
        messages=[{"role": "user", "content": user_prompt(lines, kind, lang, count, seconds, duration, topic)}],
    )
    billed = getattr(resp, "model", None) or model
    try:
        tokens, usd = usage_cost(pricing, billed, resp.usage)
    except KeyError:
        tokens, usd = usage_cost(pricing, model, resp.usage)
    if usage_log:
        usage_log({"model": billed, **tokens, "usd": round(usd, 6)})
    if resp.stop_reason == "refusal":
        return [], "Claude отказался писать сценарий по этому мультфильму — попробуй свой текст"
    if resp.stop_reason == "max_tokens":
        return [], "ответ Claude обрезан — попробуй меньше роликов за раз"
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        items = json.loads(text)["scripts"]
    except (ValueError, KeyError):
        return [], "ответ Claude не разобран"
    scripts = clean_scripts(items, duration)
    return scripts, None if scripts else "Claude вернул пустые сценарии"


_WORD = re.compile(r"[a-zа-яё0-9']+", re.I)


def _tokens(s):
    return {w for w in _WORD.findall((s or "").lower().replace("ё", "е")) if len(w) > 3}


def split_sentences(text):
    parts = re.split(r"(?<=[.!?…])\s+|\n+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def manual_script(text, lines, duration, title="", lang="ru"):
    """Свой текст без AI: каждая фраза -> сцена мультфильма с самыми похожими словами;
    если совпадений нет — следующая по порядку сцена (история идёт вперёд)."""
    sentences = split_sentences(text)
    if not sentences:
        raise ValueError("Пустой текст.")
    step = max(3.0, (duration or 60) / (len(sentences) + 1))
    out, pos, used = [], 0.0, set()
    for sent in sentences:
        toks = _tokens(sent)
        best, score = None, 0.0
        for i, (s, e, t) in enumerate(lines):
            if i in used:
                continue
            lt = _tokens(t)
            if toks and lt:
                sc = len(toks & lt) / len(toks | lt)
                if sc > score:
                    best, score = i, sc
        if best is not None and score >= 0.15:
            used.add(best)
            a, b = lines[best][0], max(lines[best][1], lines[best][0] + 3)
            pos = b
        else:
            a = min(pos, max(0.0, (duration or 60) - step))
            b = a + step
            pos = b
        out.append({"text": sent, "from": round(a, 2), "to": round(min(b, duration or b), 2)})
    first = sentences[0]
    return {"title": title or first[:90], "kind": "recap", "overlay": "", "why": "свой текст", "lines": out}


# ---------- без API: задание для обычного чата Claude (claude.ai) и разбор ответа ----------

CHAT_FORMAT = """Формат ответа — строго такой, без пояснений до и после:

### Название ролика
Надпись: короткая надпись сверху кадра
[12.5-16.0] Первая фраза сценария.
[20.1-24.8] Вторая фраза сценария.

### Название следующего ролика
…

В квадратных скобках — отрезок мультфильма в секундах (из расшифровки), который показывает то, о чём фраза."""


def chat_prompt(lines, kind, lang, count, seconds, duration, topic=""):
    """Текст, который пользователь вставит в чат Claude вместо платного API-запроса."""
    return SYSTEM + "\n\n" + CHAT_FORMAT + "\n\n" + user_prompt(lines, kind, lang, count, seconds, duration, topic)


_TIMED = re.compile(r"^\s*[\[(]?\s*(\d+(?:[.,]\d+)?)\s*(?:с|s)?\s*[–—-]\s*(\d+(?:[.,]\d+)?)\s*(?:с|s)?\s*[\])]?\s*[:|.—-]?\s*(.+)$")
_OVERLAY = re.compile(r"^\s*(надпись|overlay)\s*:\s*(.+)$", re.I)


def parse_chat_answer(text, lines, duration):
    """Ответ из чата (или свой текст) -> список сценариев.

    Понимает «### Название», «Надпись: …» и строки «[12.5-16] фраза». Строки без таймкодов
    привязываются к сценам по совпадению слов (как manual_script)."""
    blocks, cur = [], None
    for raw in (text or "").splitlines():
        s = raw.strip().strip("*").strip()
        if not s:
            continue
        if s.startswith("#"):
            cur = {"title": s.lstrip("#").strip().strip("*_").strip()[:100], "overlay": "", "items": []}
            blocks.append(cur)
            continue
        if cur is None:
            cur = {"title": "", "overlay": "", "items": []}
            blocks.append(cur)
        m = _OVERLAY.match(s)
        if m:
            cur["overlay"] = m.group(2).strip()[:60]
            continue
        cur["items"].append(s)
    out = []
    for b in blocks:
        if not b["items"]:
            continue
        timed = [_TIMED.match(x) for x in b["items"]]
        if all(timed):
            body = {"title": b["title"] or timed[0].group(3)[:90], "kind": "recap", "overlay": b["overlay"],
                    "why": "свой текст", "lines": [{"text": m.group(3).strip(), "from": float(m.group(1).replace(",", ".")),
                                                    "to": float(m.group(2).replace(",", "."))} for m in timed]}
            out += clean_scripts([body], duration)
        else:
            body = manual_script(" ".join(_TIMED.sub(r"\3", x) for x in b["items"]), lines, duration, b["title"])
            body["overlay"] = b["overlay"]
            out.append(body)
    if not out:
        raise ValueError("Не нашёл текст сценария.")
    return out
