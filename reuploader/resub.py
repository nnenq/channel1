"""Замена вшитых субтитров: найти полосы со старым текстом -> убрать -> вшить наши анимированные.

analyze()  — по кадрам (2 в секунду, уменьшенным) ищет, где картинка («контент», без чёрных и
             размытых полей) и в каких строках постоянно появляется текст с обводкой (субтитры,
             надписи-заголовки). Без OCR: текст = светлые (белые/жёлтые) штрихи с тёмной обводкой,
             много чередований по горизонтали, и стоит в одном месте у многих кадров.
Три способа (method):
  erase — «стереть»: буквы старых субтитров (светлые с тёмной обводкой) стираются в каждом кадре,
          картинка под ними дорисовывается по соседним пикселям (inpainting, OpenCV); наши субтитры
          встают на то же место; размер кадра не меняется (по умолчанию);
  strip — «полоска»: старые субтитры закрываются размытой полосой на всю ширину кадра, наши
          субтитры — по центру этой полосы; остальной кадр и размер видео не меняются;
  crop  — «обрезка»: полоса с текстом вырезается (plan), кадр собирается в вертикальное 1080×1920
          на размытом фоне, наши субтитры — как обычно.
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
METHODS = ("erase", "strip", "crop")


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


def _tail(subs_name=None, enhance=False):
    from .music import ENHANCE

    return ("," + ENHANCE if enhance else "") + (f",ass={subs_name}" if subs_name else "") + ",format=yuv420p[v]"


def _sound(voice, music_idx, duration, look):
    """-> (доп. входы ffmpeg, звуковой граф или None). look: music, start, level."""
    from . import music as mu

    look = look or {}
    track = look.get("music")
    graph = mu.audio_graph(voice, music_idx if track else None, duration, look.get("level", "mid"))
    return (mu.music_input(track, look.get("start", 0.0)) if track else []), graph


def build_filter(lay, subs_name=None, enhance=False):
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
    return fc + _tail(subs_name, enhance)


def caption_strip(lay):
    """Главная полоса со старыми субтитрами (нижняя в картинке) — её закрываем и туда ставим наши.
    -> (y0, y1, размер шрифта) или None. Полоса расширяется, чтобы в неё влезли наши субтитры."""
    if not lay.bands:
        return None
    c0, c1 = lay.content
    inside = [bd for bd in lay.bands if c0 <= (bd[0] + bd[1]) / 2 <= c1] or lay.bands
    a, b = max(inside, key=lambda bd: bd[1])
    size = max(18, round(lay.height * 0.045))
    need = round(size * 1.9)
    if b - a < need:
        mid = (a + b) / 2
        a, b = int(mid - need / 2), int(mid + need / 2)
    a, b = max(0, a), min(lay.height, b)
    return a // 2 * 2, b // 2 * 2, size


def strip_filter(lay, strips, subs_name=None, enhance=False):
    """Размытые полосы на всю ширину поверх старого текста; размер кадра не меняется."""
    w, h = lay.width // 2 * 2, lay.height // 2 * 2
    chain = f"[0:v]crop={w}:{h}:0:0,setsar=1"
    if strips:
        n = len(strips)
        chain += f",split={n + 1}[base]" + "".join(f"[s{i}]" for i in range(n)) + ";"
        prev = "base"
        for i, (a, b) in enumerate(strips):
            hh = max(2, (min(b, h) - a) // 2 * 2)
            chain += (f"[s{i}]crop={w}:{hh}:0:{a},gblur=sigma=22:steps=3,eq=brightness=-0.04[b{i}];"
                      f"[{prev}][b{i}]overlay=0:{a}[m{i}];")
            prev = f"m{i}"
        chain += f"[{prev}]null"
    return chain + _tail(subs_name, enhance)


def letters_mask(band, width):
    """Маска букв в полосе кадра (BGR): светлые пятна, почти целиком обведённые тёмным,
    вместе с обводкой и тенью. -> uint8 0/1."""
    import cv2

    im = band.astype(np.int16)
    b, g, r = im[..., 0], im[..., 1], im[..., 2]
    bright = ((r > 185) & (g > 165)).astype(np.uint8)        # белые и жёлтые буквы
    dark = (im.max(2) < 90).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(bright, 8, cv2.CV_32S)
    if n <= 1:
        return np.zeros_like(bright)
    # кольцо вокруг каждого пятна: соседние пиксели получают номер пятна (максимум по соседям)
    grown = cv2.dilate(lab.astype(np.float32), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))).astype(np.int32)
    ring = (bright == 0) & (grown > 0)
    total = np.bincount(grown[ring], minlength=n)
    darkn = np.bincount(grown[ring & (dark == 1)], minlength=n)
    area, h, w = st[:, cv2.CC_STAT_AREA], st[:, cv2.CC_STAT_HEIGHT], st[:, cv2.CC_STAT_WIDTH]
    good = (area >= 15) & (h <= 0.9 * band.shape[0]) & (w <= 0.25 * width) & (total > 0) \
        & (darkn >= 0.55 * np.maximum(total, 1))
    good[0] = False
    letters = good[lab].astype(np.uint8)
    rad = max(4, round(width * 0.011))
    return cv2.dilate(letters, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * rad + 1, 2 * rad + 1)))


def erase(band, mask):
    """Дорисовывает картинку под маской. Считаем в половинном размере (в 10 раз быстрее, на вид
    так же) и вклеиваем только стёртые пиксели — остальной кадр не трогаем."""
    import cv2

    h, w = band.shape[:2]
    small = cv2.resize(band, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
    ms = cv2.dilate(cv2.resize(mask, (w // 2, h // 2), interpolation=cv2.INTER_NEAREST), np.ones((3, 3), np.uint8))
    fill = cv2.resize(cv2.inpaint(small, ms * 255, 3, cv2.INPAINT_TELEA), (w, h), interpolation=cv2.INTER_LINEAR)
    return np.where(mask[..., None] > 0, fill, band)


def erase_render(src, out, lay, bands, subs=None, progress=None, look=None):
    """Стирает буквы в полосах bands в каждом кадре и кодирует видео с исходным звуком
    (look — музыка и улучшение картинки, см. replace_subtitles)."""
    try:
        import cv2
    except ImportError:
        raise RuntimeError("для способа «стереть» нужен OpenCV: pip install opencv-python-headless") from None
    info = probe(src)
    w, h = lay.width, lay.height
    frame_size = w * h * 3
    total = max(1, int(info.duration * info.fps))
    src, out = str(Path(src).resolve()), Path(out).resolve()
    cwd = subs_name = None
    if subs:
        subs = Path(subs).resolve()
        cwd, subs_name = subs.parent, subs.name
    fc = f"[0:v]crop={w // 2 * 2}:{h // 2 * 2}:0:0" + _tail(subs_name, (look or {}).get("enhance"))
    extra, graph = _sound("1:a" if info.audio_streams else None, 2, info.duration, look)
    if graph:
        fc += ";" + graph
    dec = subprocess.Popen([ffmpeg_exe(), "-v", "error", "-i", src, "-an", "-f", "rawvideo", "-pix_fmt", "bgr24",
                            "-vsync", "passthrough", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen([ffmpeg_exe(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                            "-s", f"{w}x{h}", "-r", f"{info.fps:.3f}", "-i", "-", "-i", src, *extra,
                            "-filter_complex", fc, "-map", "[v]", "-map", "[a]" if graph else "1:a?",
                            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
                            "-t", f"{info.duration:.3f}", "-movflags", "+faststart",
                            "-map_metadata", "-1", str(out)], stdin=subprocess.PIPE, cwd=cwd)
    done = 0
    try:
        while True:
            buf = dec.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3).copy()
            for a, b in bands:
                band = frame[a:b]
                mask = letters_mask(band, w)
                if mask.any():
                    frame[a:b] = erase(band, mask)
            enc.stdin.write(frame.tobytes())
            done += 1
            if progress and done % 15 == 0:
                progress(min(1.0, done / total))
    finally:
        enc.stdin.close()
        dec.stdout.close()
        dec.wait()
        enc.wait()
    if enc.returncode != 0 or not out.exists():
        raise RuntimeError("ffmpeg не смог собрать видео")
    return out


def render(src, out, lay, subs=None, progress=None, strips=None, look=None):
    """strips=None — способ «обрезка» (вертикальное 1080×1920); список полос — способ «полоска»."""
    from . import ffprog

    src, out = str(Path(src).resolve()), Path(out).resolve()
    cwd = subs_name = None
    if subs:
        subs = Path(subs).resolve()
        cwd, subs_name = subs.parent, subs.name
    enhance = (look or {}).get("enhance")
    fc = strip_filter(lay, strips, subs_name, enhance) if strips is not None else build_filter(lay, subs_name, enhance)
    info = probe(src)
    extra, graph = _sound("0:a" if info.audio_streams else None, 1, info.duration, look)
    if graph:
        fc += ";" + graph
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", src, *extra,
           "-filter_complex", fc, "-map", "[v]", "-map", "[a]" if graph else "0:a?",
           "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
           "-t", f"{info.duration:.3f}", "-movflags", "+faststart", "-map_metadata", "-1", str(out)]
    ffprog.run(cmd, info.duration, progress, cwd=cwd)
    return out


def replace_subtitles(src, out, transcriber, work_dir, progress=None, method="erase", music=None,
                      music_level="mid", enhance=False):
    """Всё вместе. progress(этап, доля). music — путь к фоновому треку (или None),
    music_level — low|mid|high, enhance — чуть ярче цвета и резкость. -> dict для отчёта."""
    from .music import start_offset
    from .subtitles import to_ass

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda *_: None)
    say("ищу старые субтитры", 0.02)
    lay = analyze(src)
    say("распознаю речь", 0.1)
    words = transcriber(src)
    build = lambda f: say("собираю видео", 0.4 + 0.6 * f)   # noqa: E731
    look = {"enhance": enhance, "level": music_level}
    if music:
        look.update(music=str(music), start=start_offset(probe(music).duration, probe(src).duration))
    if method == "crop":
        subs = to_ass(words, OUT_W, OUT_H, work / "subs.ass") if words else None
        render(src, out, lay, subs, progress=build, look=look)
        mode = lay.mode
    elif method == "erase":
        main = caption_strip(lay)
        w, h = lay.width // 2 * 2, lay.height // 2 * 2
        if main:
            a, b, size = main
            subs = to_ass(words, w, h, work / "subs.ass", size=size,
                          margin_v=max(0, round(h - (a + b) / 2 - size * 0.55))) if words else None
        else:
            subs = to_ass(words, w, h, work / "subs.ass") if words else None
        erase_render(src, out, lay, [(a // 2 * 2, b) for a, b in lay.bands], subs, progress=build, look=look)
        mode = "erase" if lay.bands else "clean"
    else:
        main = caption_strip(lay)
        strips = [(a, b) for a, b in lay.bands if not main or b <= main[0] or a >= main[1]]
        w, h = lay.width // 2 * 2, lay.height // 2 * 2
        if main:
            a, b, size = main
            strips.append((a, b))
            # наш текст — по центру полосы (выравнивание по низу строки)
            subs = to_ass(words, w, h, work / "subs.ass", size=size,
                          margin_v=max(0, round(h - (a + b) / 2 - size * 0.55))) if words else None
        else:
            subs = to_ass(words, w, h, work / "subs.ass") if words else None
        render(src, out, lay, subs, progress=build, strips=sorted(strips), look=look)
        mode = "strip" if main else "clean"
    return {"mode": mode, "method": method, "bands": lay.bands, "content": lay.content, "keep": lay.keep,
            "words": len(words), "size": [lay.width, lay.height],
            "music": Path(music).name if music else None, "enhance": bool(enhance)}
