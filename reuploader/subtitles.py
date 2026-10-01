"""Субтитры «как в Shorts»: короткие фразы по 1–3 слова крупным шрифтом с обводкой.

Анимация (animated=True): фраза появляется с «прыжком» (увеличение 70% → 108% → 100%),
а слово, которое звучит сейчас, подсвечено жёлтым и чуть крупнее.
Слова с таймкодами — из whisper (smartcut.analyze.Word: start, end, text).
Результат — .ass-файл, который ffmpeg вшивает в видео (effects.apply_effects).
"""
MAX_WORDS = 3
MAX_CHARS = 18
BREAK_PAUSE = 0.4       # пауза между словами, после которой начинается новая фраза
MIN_SHOW = 0.35         # фраза на экране не меньше, сек
HIGHLIGHT = "&H0000E5FF&"   # жёлтый (в ASS цвет пишется как BGR)
POP = r"\fscx70\fscy70\t(0,90,\fscx108\fscy108)\t(90,160,\fscx100\fscy100)"


def groups(words):
    """[(start, end, [Word])] — слова, сгруппированные в короткие фразы, с временем показа."""
    out, cur = [], []
    for w in words:
        text = (w.text or "").strip()
        if not text:
            continue
        if cur and (len(cur) >= MAX_WORDS or w.start - cur[-1].end > BREAK_PAUSE
                    or len(" ".join(x.text.strip() for x in cur + [w])) > MAX_CHARS):
            out.append(cur)
            cur = []
        cur.append(w)
        if text[-1] in ".!?…":
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    res = []
    for i, group in enumerate(out):
        start, end = group[0].start, max(group[-1].end, group[0].start + MIN_SHOW)
        if i + 1 < len(out):
            end = min(end + 0.15, out[i + 1][0].start)
        res.append((start, max(end, start + 0.05), group))
    return res


def chunks(words):
    """[(start, end, text)] — фразы текстом."""
    return [(s, e, " ".join(w.text.strip() for w in g)) for s, e, g in groups(words)]


def _ts(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(text):
    # фигурные скобки и обратный слэш — служебные символы ASS
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").upper()


def _animated_events(start, end, group):
    """Одна строка на каждое слово фразы: весь текст, текущее слово подсвечено.
    Первая строка фразы — с «прыжком» появления."""
    events = []
    for k, w in enumerate(group):
        s = start if k == 0 else max(start, w.start)
        e = group[k + 1].start if k + 1 < len(group) else end
        e = min(max(e, s + 0.05), end)
        if e <= s:
            continue
        parts = []
        for j, x in enumerate(group):
            t = _clean(x.text.strip())
            parts.append(rf"{{\c{HIGHLIGHT}\fscx112\fscy112}}{t}{{\r}}" if j == k else t)
        text = (f"{{{POP}}}" if k == 0 else "") + " ".join(parts)
        events.append((s, e, text))
    return events


def to_ass(words, width, height, path, animated=True, overlay=None, overlay_until=None, size=None, margin_v=None):
    """Пишет .ass для видео width×height. -> path или None, если слов нет.
    overlay — надпись-крючок сверху кадра (до overlay_until секунд, по умолчанию — до конца).
    size, margin_v — свой размер шрифта и отступ снизу (по умолчанию 4,5 % и 28 % высоты)."""
    lines = groups(words)
    if not lines and not overlay:
        return None
    w, h = width or 1080, height or 1920
    size = size or round(h * 0.045)
    margin_v = round(h * 0.28) if margin_v is None else margin_v     # выше нижнего интерфейса Shorts
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,1,0,1,{max(2, size // 12)},2,2,{round(w * 0.08)},{round(w * 0.08)},{margin_v},1
Style: Top,Arial,{round(size * 0.95)},&H0000E5FF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,{max(6, size // 5)},0,8,{round(w * 0.06)},{round(w * 0.06)},{round(h * 0.09)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    if overlay:
        end = overlay_until or (lines[-1][1] if lines else 60)
        events.append((0.0, end, "{\\fad(250,0)}" + _clean(overlay).replace("  ", " ")))
    for s, e, g in lines:
        if animated:
            events += _animated_events(s, e, g)
        else:
            events.append((s, e, _clean(" ".join(x.text.strip() for x in g))))
    body = "".join(f"Dialogue: 0,{_ts(s)},{_ts(e)},{'Top' if i == 0 and overlay else 'Cap'},,0,0,0,,{t}\n"
                   for i, (s, e, t) in enumerate(events))
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + body)
    return path
