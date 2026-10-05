"""Замена вшитых субтитров: найти полосы со старым текстом -> убрать -> вшить наши анимированные.

analyze()  — по кадрам (2 в секунду, уменьшенным) ищет, где картинка («контент», без чёрных и
             размытых полей) и в каких строках постоянно появляется текст с обводкой (субтитры,
             надписи-заголовки). Без OCR: текст = светлые (белые/жёлтые) штрихи с тёмной обводкой,
             много чередований по горизонтали, и стоит в одном месте у многих кадров.
Способы (method):
  capcut — «как в CapCut»: остаётся только картинка мультика (надписи на полях отрезаются, поверх
          картинки — стираются), вертикальный кадр 1080×1920 на размытом фоне из той же картинки;
  keep  — кадр не трогать (только наши субтитры и музыка);
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
METHODS = ("capcut", "erase", "strip", "crop", "keep")


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


# HDR с iPhone -> обычные цвета (SDR, BT.709); без этого картинка блёклая или серая
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
           "zscale=t=bt709:m=bt709:r=tv,format=yuv420p,")
# готовое видео всегда помечено как обычное SDR — иначе телефон может показать его серым
SDR_TAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]


def check_video(path):
    """Готовый файл должен содержать картинку, а не только звук."""
    info = probe(path)
    if not info.width or not info.height:
        raise RuntimeError("в готовом файле нет картинки — видео не отправляю")


def pre(info):
    """Начало видеофильтра для исходника."""
    return TONEMAP if info.hdr else ""


def _frames(src, every=1 / SCAN_FPS, limit=240):
    info = probe(src)
    w, h = info.width, info.height
    sh = max(2, int(round(SCAN_W * h / w / 2)) * 2)
    fps = 1 / max(every, info.duration / limit) if info.duration else 1 / every
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(src), "-an", "-vf",
                          f"{pre(info)}fps={fps:.4f},scale={SCAN_W}:{sh}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
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

    return (("," + ENHANCE if enhance else "") + (f",ass={subs_name}" if subs_name else "")
            + ",setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709,format=yuv420p[v]")


def _sound(voice, music_idx, duration, look):
    """-> (доп. входы ffmpeg, звуковой граф или None). look: music, start, level."""
    from . import music as mu

    look = look or {}
    track = look.get("music")
    graph = mu.audio_graph(voice, music_idx if track else None, duration, look.get("level", "mid"))
    return (mu.music_input(track, look.get("start", 0.0)) if track else []), graph


def build_filter(lay, subs_name=None, enhance=False, head=""):
    y0, y1 = lay.keep
    chain = f"[0:v]{head}crop={lay.width // 2 * 2}:{y1 - y0}:0:{y0},setsar=1"
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


def strip_filter(lay, strips, subs_name=None, enhance=False, head=""):
    """Размытые полосы на всю ширину поверх старого текста; размер кадра не меняется."""
    w, h = lay.width // 2 * 2, lay.height // 2 * 2
    chain = f"[0:v]{head}crop={w}:{h}:0:0,setsar=1"
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


def _components(band, width, dark_thr=90, frac=0.55, contrast=False):
    """Светлые пятна полосы и признак «обведено тёмным» для каждого. -> (n, lab, stats, outlined)."""
    import cv2

    im = band.astype(np.int16)
    bright = ((im[..., 2] > 185) & (im[..., 1] > 165)).astype(np.uint8)        # белые и жёлтые буквы
    dark = (im.max(2) < dark_thr).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(bright, 8, cv2.CV_32S)
    if n <= 1:
        return n, lab, st, np.zeros(n, bool)
    # кольцо вокруг каждого пятна: соседние пиксели получают номер пятна (максимум по соседям)
    grown = cv2.dilate(lab.astype(np.float32), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))).astype(np.int32)
    ring = (bright == 0) & (grown > 0)
    total = np.bincount(grown[ring], minlength=n)
    darkn = np.bincount(grown[ring & (dark == 1)], minlength=n)
    outlined = darkn >= frac * np.maximum(total, 1)
    if contrast:      # обводка не чёрная, а просто заметно темнее буквы (серая, полупрозрачная тень)
        luma = im[..., 0] * 0.11 + im[..., 1] * 0.59 + im[..., 2] * 0.30
        ring_l = np.bincount(grown[ring], weights=luma[ring], minlength=n) / np.maximum(total, 1)
        in_l = np.bincount(lab.ravel(), weights=luma.ravel(), minlength=n) / np.maximum(st[:, 4], 1)
        outlined |= (ring_l < 0.45 * in_l) & (darkn >= 0.25 * total)
    ok = (st[:, 4] >= 15) & (st[:, 2] <= 0.25 * width) & (total > 0) & outlined
    ok[0] = False
    return n, lab, st, ok


def _finish(letters, width, count, line=True):
    import cv2

    rad = max(4, round(width * 0.011))
    mask = cv2.dilate(letters, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * rad + 1, 2 * rad + 1)))
    if line and count >= 2:
        # промежутки между буквами и словами одной строки тоже стираем (не дальше ~7 % ширины кадра):
        # если какую-то букву не узнали, она не «мигает»
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, round(width * 0.07)) | 1, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    return mask


def letters_mask(band, width, geo=None, line=True):
    """Маска старых субтитров в полосе кадра (BGR). -> uint8 0/1.

    Буква — светлое (белое/жёлтое) пятно, почти целиком обведённое тёмным. geo — выученная по ролику
    геометрия строки (высота букв и где строка, см. learn_geometry): с ней узнаются и буквы с серой/
    тонкой обводкой, а пятна другого размера (жёлтый Губка Боб) не трогаются."""
    if geo:
        hlo, hhi, cy0, spread = geo
        n, lab, st, ok = _components(band, width, 110, 0.45, contrast=True)
        hh = st[:, 3]
        cy = st[:, 1] + hh / 2
        # высота — как у букв субтитров (с запасом на «выскакивающее» слово), строка — основная или
        # соседняя сверху/снизу (длинная фраза переносится на две строки)
        ok &= (hh >= 0.5 * hlo) & (hh <= 1.6 * hhi) & (np.abs(cy - cy0) <= max(1.7 * hhi, 2.5 * spread))
        if ok.any():
            # остатки текста рядом со строкой (полустёртые буквы прошлой замены субтитров, края
            # «выскакивающего» слова) — светлые пятна размером с букву, касающиеся найденного текста
            import cv2

            near = cv2.dilate(ok[lab].astype(np.uint8),
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(hhi) | 1, int(hhi) | 1)))
            touch = np.bincount(lab[near > 0], minlength=n) > 0
            ok |= touch & (st[:, 4] >= 15) & (hh <= 1.6 * hhi) & (st[:, 2] <= 0.25 * width)
            ok[0] = False
    else:
        n, lab, st, ok = _components(band, width)
        ok &= st[:, 3] <= 0.9 * band.shape[0]
    return _finish(ok[lab].astype(np.uint8), width, int(ok.sum()), line)


def widen(bands, geos, height):
    """Расширяет полосы на строку вверх и вниз — для субтитров в две строки. -> (полосы, геометрии)."""
    out_b, out_g = [], []
    for (a, b), g in zip(bands, geos):
        if not g:
            out_b.append((a, b))
            out_g.append(g)
            continue
        d = round(1.6 * g[1])
        na, nb = max(0, (a - d) // 2 * 2), min(height, b + d)
        out_b.append((na, nb))
        out_g.append((g[0], g[1], g[2] + (a - na), g[3]))
    return out_b, out_g


def learn_geometry(src, info, bands, width, height, samples=160):
    """Высота букв и положение строки старых субтитров в каждой полосе — по уверенным кадрам
    (где в ряд стоят хотя бы 2 буквы одной высоты). -> [geo | None] по полосам."""
    fps = min(4.0, samples / max(info.duration, 1))
    p = subprocess.Popen([ffmpeg_exe(), "-hide_banner", "-v", "error", "-i", str(src), "-an",
                          "-vf", f"{pre(info)}fps={fps:.3f}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                         stdout=subprocess.PIPE)
    stats = [([], []) for _ in bands]
    size = width * height * 3
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(height, width, 3)
            for (a, b), (hs, cys) in zip(bands, stats):
                n, lab, st, ok = _components(frame[a:b], width)
                idx = np.where(ok & (st[:, 3] <= 0.9 * (b - a)))[0]
                cy, hh, cx = st[idx, 1] + st[idx, 3] / 2, st[idx, 3], st[idx, 0] + st[idx, 2] / 2
                for i in range(len(idx)):
                    same = (np.abs(cy - cy[i]) < 0.35 * np.maximum(hh, hh[i])) & (hh > 0.5 * hh[i]) \
                        & (hh < 2 * hh[i]) & (np.abs(cx - cx[i]) < 4 * np.maximum(hh, hh[i]))
                    if same.sum() >= 3:
                        hs.append(hh[i])
                        cys.append(cy[i])
    finally:
        p.stdout.close()
        p.wait()
    out = []
    for hs, cys in stats:
        out.append((float(np.percentile(hs, 25)), float(np.percentile(hs, 90)), float(np.median(cys)),
                    float(np.std(cys))) if len(hs) >= 30 else None)
    return out


def erase(band, mask):
    """Дорисовывает картинку под маской. Считаем в половинном размере (в 10 раз быстрее, на вид
    так же) и вклеиваем только стёртые пиксели — остальной кадр не трогаем."""
    import cv2

    h, w = band.shape[:2]
    small = cv2.resize(band, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
    ms = cv2.dilate(cv2.resize(mask, (w // 2, h // 2), interpolation=cv2.INTER_NEAREST), np.ones((3, 3), np.uint8))
    fill = cv2.resize(cv2.inpaint(small, ms * 255, 3, cv2.INPAINT_TELEA), (w, h), interpolation=cv2.INTER_LINEAR)
    return np.where(mask[..., None] > 0, fill, band)


def erase_render(src, out, lay, bands, subs=None, progress=None, look=None, layout=None):
    """Стирает буквы в полосах bands в каждом кадре и кодирует видео с исходным звуком
    (look — музыка и улучшение картинки, см. replace_subtitles). layout — Layout для вертикальной
    компоновки (как «Обрезать полосу»/«Как в CapCut»); без него размер кадра не меняется."""
    try:
        import cv2
    except ImportError:
        raise RuntimeError("для способа «стереть» нужен OpenCV: pip install opencv-python-headless") from None
    info = probe(src)
    w, h = lay.width, lay.height
    frame_size = w * h * 3
    total = max(1, int(info.duration * min(60.0, info.fps or 30.0)))
    src, out = str(Path(src).resolve()), Path(out).resolve()
    cwd = subs_name = None
    if subs:
        subs = Path(subs).resolve()
        cwd, subs_name = subs.parent, subs.name
    fc = (build_filter(layout, subs_name, (look or {}).get("enhance")) if layout else
          f"[0:v]crop={w // 2 * 2}:{h // 2 * 2}:0:0" + _tail(subs_name, (look or {}).get("enhance")))
    geos = learn_geometry(src, info, bands, w, h) if bands else []
    bands, geos = widen(bands, geos, h)
    extra, graph = _sound("1:a" if info.audio_streams else None, 2, info.duration, look)
    if graph:
        fc += ";" + graph
    # ровная частота кадров: видео с телефона часто «плавающее» (VFR) — иначе картинка уезжает от звука
    fps = min(60.0, info.fps or 30.0)
    dec = subprocess.Popen([ffmpeg_exe(), "-hide_banner", "-v", "error", "-i", src, "-an", "-vf", f"{pre(info)}fps={fps:.3f}",
                            "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
    enc = subprocess.Popen([ffmpeg_exe(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                            "-s", f"{w}x{h}", "-r", f"{fps:.3f}", "-i", "-", "-i", src, *extra,
                            "-filter_complex", fc, "-map", "[v]", "-map", "[a]" if graph else "1:a?",
                            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
                            "-t", f"{info.duration:.3f}", "-movflags", "+faststart", *SDR_TAGS,
                            "-map_metadata", "-1", str(out)], stdin=subprocess.PIPE, cwd=cwd)
    done = 0
    window = {}                 # номер кадра -> (кадр, маски полос). Маска кадра — объединение масок
    reach = 2                   # ±2 соседних кадров: буква, не узнанная в одном кадре, всё равно стирается
    next_out = 0

    def emit(j):
        frame = window[j][0].copy()
        for k, (a, b) in enumerate(bands):
            mask = np.maximum.reduce([window[i][1][k] for i in range(j - reach, j + reach + 1) if i in window])
            if mask.any():
                frame[a:b] = erase(frame[a:b], mask)
        enc.stdin.write(frame.tobytes())

    try:
        while True:
            buf = dec.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            window[done] = (frame, [letters_mask(frame[a:b], w, g) for (a, b), g in zip(bands, geos)])
            while next_out + reach <= done:
                emit(next_out)
                next_out += 1
                window.pop(next_out - reach - 1, None)
            done += 1
            if progress and done % 15 == 0:
                progress(min(1.0, done / total))
        while next_out < done:                                       # хвост ролика
            emit(next_out)
            next_out += 1
    finally:
        enc.stdin.close()
        dec.stdout.close()
        dec_err = dec.stderr.read().decode("utf-8", "replace").strip()
        dec.wait()
        enc.wait()
    if not done:      # кадры не прочитались — не отдаём «видео» без картинки
        raise RuntimeError("не удалось прочитать кадры видео" + (f": {dec_err[-300:]}" if dec_err else ""))
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
    info = probe(src)
    fc = (strip_filter(lay, strips, subs_name, enhance, pre(info)) if strips is not None
          else build_filter(lay, subs_name, enhance, pre(info)))
    extra, graph = _sound("0:a" if info.audio_streams else None, 1, info.duration, look)
    if graph:
        fc += ";" + graph
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", src, *extra,
           "-filter_complex", fc, "-map", "[v]", "-map", "[a]" if graph else "0:a?",
           "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
           "-t", f"{info.duration:.3f}", "-movflags", "+faststart", *SDR_TAGS, "-map_metadata", "-1", str(out)]
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
    if method == "capcut":
        # как в CapCut: «Кадрирование» — только картинка мультика (надписи на полях сверху/снизу
        # отрезаются), «Холст: размытие» — вертикальный кадр на размытом фоне; надписи поверх
        # картинки стираются
        from dataclasses import replace as _dc_replace

        c0, c1 = lay.content
        frame = _dc_replace(lay, keep=(c0 // 2 * 2, max(c0 // 2 * 2 + 2, c1 // 2 * 2)), blur=[])
        inside = [(max(a, c0), min(b, c1)) for a, b in lay.bands if b > c0 and a < c1]
        subs = to_ass(words, OUT_W, OUT_H, work / "subs.ass") if words else None
        if inside:
            erase_render(src, out, lay, [(a // 2 * 2, b) for a, b in inside], subs, progress=build, look=look,
                         layout=frame)
        else:
            render(src, out, frame, subs, progress=build, look=look)
        mode = "capcut"
    elif method == "keep":
        # кадр не трогаем: только наши субтитры / музыка
        w, h = lay.width // 2 * 2, lay.height // 2 * 2
        subs = to_ass(words, w, h, work / "subs.ass") if words else None
        render(src, out, lay, subs, progress=build, strips=[], look=look)
        mode = "keep"
    elif method == "crop":
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
    check_video(out)
    return {"mode": mode, "method": method, "bands": lay.bands, "content": lay.content, "keep": lay.keep,
            "words": len(words), "size": [lay.width, lay.height],
            "music": Path(music).name if music else None, "enhance": bool(enhance)}
