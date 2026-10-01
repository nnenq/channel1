"""Замена вшитых субтитров: найти полосы со старым текстом -> убрать -> вшить наши анимированные.

analyze()  — по кадрам (2 в секунду, уменьшенным) ищет, где картинка («контент», без чёрных и
             размытых полей) и в каких строках постоянно появляется текст с обводкой (субтитры,
             надписи-заголовки). Без OCR: текст = светлые (белые/жёлтые) штрихи с тёмной обводкой,
             много чередований по горизонтали, и стоит в одном месте у многих кадров.
plan()     — что оставить: если без полос с текстом остаётся большой кусок картинки — обрезаем по
             нему (чисто); иначе размываем полосы.
render()   — вертикальное 1080×1920: кадр по центру, размытый фон, наши субтитры.
"""
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .ffmpeg_path import ffmpeg_exe
from .smartcut.media import probe

OUT_W, OUT_H = 1080, 1920
SCAN_W = 360
SCAN_FPS = 2
TEXT_FREQ = 0.2          # строка «с текстом», если текст в ней в ≥20 % кадров
MIN_KEEP = 0.6           # обрезаем, если остаётся ≥60 % картинки; иначе размываем полосы


@dataclass
class Layout:
    width: int
    height: int
    content: tuple                  # (y0, y1) картинки в пикселях исходника
    bands: list = field(default_factory=list)   # [(y0, y1)] полосы с текстом
    keep: tuple = None              # что оставить (y0, y1)
    blur: list = field(default_factory=list)    # полосы, которые размыть (внутри keep)

    @property
    def mode(self):
        return "blur" if self.blur else ("crop" if self.keep != self.content else "clean")


def _frames(src, every=1 / SCAN_FPS, limit=240):
    info = probe(src)
    w, h = info.width, info.height
    sh = max(2, int(round(SCAN_W * h / w / 2)) * 2)
    fps = 1 / max(every, info.duration / limit) if info.duration else 1 / every
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(src), "-an", "-vf",
                          f"fps={fps:.4f},scale={SCAN_W}:{sh}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    n = len(raw) // (SCAN_W * sh * 3)
    return w, h, np.frombuffer(raw[: n * SCAN_W * sh * 3], np.uint8).reshape(n, sh, SCAN_W, 3)


def _runs(mask, min_len=1, max_gap=0):
    """Непрерывные участки True -> [(a, b)] (b не включительно); дыры до max_gap склеиваются."""
    runs, start = [], None
    for i, v in enumerate(list(mask) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append([start, i])
            start = None
    merged = []
    for a, b in runs:
        if merged and a - merged[-1][1] <= max_gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= min_len]


def text_rows(frame, reach=4):
    """Строки кадра, похожие на текст с обводкой. -> bool[H].

    Буква — тонкий светлый (белый/жёлтый) штрих, у которого тёмная обводка близко и слева, и справа.
    Белые глаза и зубы героев мультфильмов — широкие пятна, их середина далеко от обводки."""
    im = frame.astype(np.int16)
    r, g, b = im[..., 0], im[..., 1], im[..., 2]
    bright = ((r > 200) & (g > 200) & (b > 200)) | ((r > 200) & (g > 160) & (b < 120))
    dark = im.max(2) < 80
    left, right = np.zeros_like(dark), np.zeros_like(dark)
    for d in range(1, reach + 1):
        left |= np.roll(dark, d, 1)
        right |= np.roll(dark, -d, 1)
    m = bright & left & right
    w = m.shape[1]
    m[:, : w // 20] = False
    m[:, w - w // 20:] = False
    return m.sum(1) >= max(4, round(w * 0.017))


def content_rows(frames):
    """Где живая картинка: без чёрных/размытых полей и без неподвижных надписей по краям
    (заголовок канала над кадром). -> (y0, y1) в строках скана."""
    g = frames.mean(3)
    grad = np.abs(np.diff(g, axis=2)).mean((0, 2))
    grad = np.convolve(grad, np.ones(3) / 3, "same")
    thr = 0.35 * np.percentile(grad, 90)
    runs = _runs(grad > thr, max_gap=max(2, len(grad) // 50))
    if not runs:
        return 0, len(grad)
    c0, c1 = max(runs, key=lambda ab: ab[1] - ab[0])
    if len(frames) >= 4:
        std = g.std(0).mean(1)
        med = float(np.median(std[c0:c1]))
        if med > 8:                          # видео живое — срезаем неподвижные края
            limit = (c1 - c0) // 4
            dead = (std < 0.6 * med) | (grad < thr)          # неподвижно или пусто
            top = [i for i in range(c0, c0 + limit) if dead[i]]
            bottom = [i for i in range(c1 - limit, c1) if dead[i]]
            a = top[-1] + 1 if top else c0
            b = bottom[0] if bottom else c1
            c0, c1 = a, b
    return c0, c1


def analyze(src):
    w, h, frames = _frames(src)
    sh = frames.shape[1]
    k = h / sh
    c0, c1 = content_rows(frames)
    freq = np.mean([text_rows(f) for f in frames], axis=0)
    pad = max(2, sh // 70)
    bands = []
    for a, b in _runs(freq >= TEXT_FREQ, min_len=max(2, sh // 120), max_gap=max(2, sh // 60)):
        bands.append((max(0, a - pad), min(sh, b + pad)))
    lay = Layout(w, h, (int(c0 * k), int(c1 * k)), [(int(a * k), int(b * k)) for a, b in bands])
    return plan(lay)


def plan(lay):
    c0, c1 = lay.content
    inside = [(max(a, c0), min(b, c1)) for a, b in lay.bands if b > c0 and a < c1]
    pieces, cur = [], c0
    for a, b in sorted(inside):
        if a > cur:
            pieces.append((cur, a))
        cur = max(cur, b)
    if cur < c1:
        pieces.append((cur, c1))
    best = max(pieces, key=lambda ab: ab[1] - ab[0]) if pieces else None
    if not inside:
        lay.keep, lay.blur = (c0, c1), []
    elif best and best[1] - best[0] >= MIN_KEEP * (c1 - c0):
        lay.keep, lay.blur = best, []
    else:
        lay.keep, lay.blur = (c0, c1), inside
    y0, y1 = lay.keep
    lay.keep = (y0 // 2 * 2, max(y0 // 2 * 2 + 2, y1 // 2 * 2))
    return lay


def build_filter(lay, subs_name=None):
    y0, y1 = lay.keep
    chain = f"[0:v]crop={lay.width // 2 * 2}:{y1 - y0}:0:{y0},setsar=1"
    parts = []
    if lay.blur:
        n = len(lay.blur)
        chain += f",split={n + 1}[base]" + "".join(f"[s{i}]" for i in range(n)) + ";"
        prev = "base"
        for i, (a, b) in enumerate(lay.blur):
            a, b = max(0, a - y0) // 2 * 2, min(y1, b) - y0
            hh = max(2, (b - a) // 2 * 2)
            parts.append(f"[s{i}]crop=iw:{hh}:0:{a},boxblur=20:3[b{i}];[{prev}][b{i}]overlay=0:{a}[m{i}];")
            prev = f"m{i}"
        chain += "".join(parts) + f"[{prev}]"
    else:
        chain += ","
    fc = (chain + f"scale={OUT_W}:-2,split[fg][bg];"
          f"[bg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
          "gblur=sigma=40,eq=brightness=-0.06[b];[b][fg]overlay=0:(H-h)/2")
    return fc + (f",ass={subs_name}" if subs_name else "") + ",format=yuv420p[v]"


def render(src, out, lay, subs=None, progress=None):
    from . import ffprog

    src, out = str(Path(src).resolve()), Path(out).resolve()
    cwd = subs_name = None
    if subs:
        subs = Path(subs).resolve()
        cwd, subs_name = subs.parent, subs.name
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
           "-filter_complex", build_filter(lay, subs_name), "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", "-map_metadata", "-1", str(out)]
    ffprog.run(cmd, probe(src).duration, progress, cwd=cwd)
    return out


def replace_subtitles(src, out, transcriber, work_dir, progress=None):
    """Всё вместе. progress(этап, доля). -> dict для отчёта."""
    from .subtitles import to_ass

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda *_: None)
    say("ищу старые субтитры", 0.02)
    lay = analyze(src)
    say("распознаю речь", 0.1)
    words = transcriber(src)
    subs = to_ass(words, OUT_W, OUT_H, work / "subs.ass") if words else None
    render(src, out, lay, subs, progress=lambda f: say("собираю видео", 0.4 + 0.6 * f))
    return {"mode": lay.mode, "bands": lay.bands, "content": lay.content, "keep": lay.keep,
            "words": len(words), "size": [lay.width, lay.height]}
