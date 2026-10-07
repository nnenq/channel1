"""«Петля» (Loop Method): конец ролика незаметно переходит в начало, и зритель смотрит второй круг.
Shorts сами крутят ролик заново — если на стыке нет паузы, затухания и «подпишись», повтор не
замечают, а YouTube видит досмотр больше 100 % и показывает ролик чаще.

Для готового ролика бот:
- убирает тишину в конце и фразы «подпишись/ставь лайк» (действие без слов, но со звуком, остаётся);
- последние доли секунды плавно переводит картинку в первый кадр — на стыке нет скачка;
- музыку не затухает долго, а лишь чуть-чуть на самом стыке.
Слова самого ролика бот не меняет — сценарии для своих роликов пишутся «петлёй» сразу (story/script.py).
"""
import re

XFADE = 0.35             # сколько секунд картинка перетекает в первый кадр
PAD = 0.25               # запас после последнего слова / начала тишины, чтобы не съесть окончание
MIN_KEEP = 0.6           # концовку «подпишись» режем, только если остаётся хотя бы 60 % ролика

OUTRO = re.compile(
    r"подпиш|подписыв|подписк|лайк|колокольч|коммент|продолжени|втор(ая|ую) част|част[ьи] ?2|до встречи|"
    r"всем пока|subscri|\blike\b|\blikes\b|follow|comment|part ?(2|two)|see you|stay tuned|bell",
    re.I)


def sentences(words, pause=0.7):
    """Слова -> фразы (списки индексов): по точке/вопросу/восклицанию или паузе."""
    out, cur = [], []
    for i, w in enumerate(words):
        if cur and w.start - words[cur[-1]].end > pause:
            out.append(cur)
            cur = []
        cur.append(i)
        if w.text.rstrip().endswith((".", "!", "?", "…")):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def quiet_tail(src, duration, noise="-38dB"):
    """С какой секунды звук в конце ролика тихий до самого конца (тишина) -> секунды или None."""
    import subprocess

    from .ffmpeg_path import ffmpeg_exe

    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(src), "-vn",
                        "-af", f"silencedetect=noise={noise}:d=0.3", "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    start = None
    for line in r.stderr.splitlines():
        if "silence_start:" in line:
            start = float(line.rsplit("silence_start:", 1)[1].split()[0])
        elif "silence_end:" in line:
            end = float(line.rsplit("silence_end:", 1)[1].split()[0])
            if end < duration - 0.15:          # тишина кончилась раньше конца ролика — это не хвост
                start = None
    return max(0.0, start) if start is not None else None


def plan_end(words, duration, quiet_from=None):
    """Где закончить ролик. quiet_from — где начинается тишина до самого конца (quiet_tail).
    Режем только тишину и фразы-призывы в конце: действие без слов, но со звуком, остаётся.
    -> (конец в секундах, вырезанная концовка-призыв или "")."""
    if not duration:
        return duration, ""
    end, dropped = duration, []
    keep = len(words or [])
    for sent in reversed(sentences(words or [])[-2:]):        # не больше двух фраз-призывов с конца
        text = " ".join(words[i].text.strip() for i in sent)
        if sent[-1] != keep - 1 or not OUTRO.search(text) or words[sent[0]].start < MIN_KEEP * duration:
            break
        keep = sent[0]
        dropped.insert(0, text)
    if dropped and keep:
        end = words[keep - 1].end + PAD
    if quiet_from is not None:
        spoken = words[keep - 1].end if keep else 0.0
        end = min(end, max(quiet_from, spoken) + PAD)
    end = min(duration, end)
    if end < max(XFADE * 4, 0.4 * duration):           # странные таймкоды — длину не трогаем
        return duration, ""
    return round(end, 3), " ".join(dropped)


def video_filter(end, fps, xfade=XFADE):
    """Хвост filter_complex: последние xfade секунд картинка перетекает в первый кадр ролика.
    Ставится в цепочку видео (начинается с запятой, заканчивается открытой цепочкой).
    Переход считается только на хвосте, остальное идёт мимо (xfade по всему ролику — вдвое дольше)."""
    x = max(0.1, min(xfade, end / 4))
    n = max(2, int(round(x * fps)) + 2)
    cut = end - x
    return (f",fps={fps:.3f},trim=0:{end:.3f},setpts=PTS-STARTPTS,split=3[lph][lpt][lpb];"
            f"[lph]trim=0:{cut:.3f}[lpm];"
            f"[lpt]trim={cut:.3f}:{end:.3f},setpts=PTS-STARTPTS,fps={fps:.3f}[lpe];"
            f"[lpb]trim=end_frame=1,loop=loop={n}:size=1:start=0,setpts=N/({fps:.3f}*TB),fps={fps:.3f}[lpf];"
            f"[lpe][lpf]xfade=transition=fade:duration={x:.3f}:offset=0[lpx];"
            f"[lpm][lpx]concat=n=2:v=1:a=0,fps={fps:.3f}")
