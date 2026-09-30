"""Уникализация видео через ffmpeg.

Цепочка: увеличение -> поворот -> обрезка до исходного размера ->
осветление теней -> цвет -> зеркало -> размытие полосок сверху и снизу ->
субтитры -> скорость.
random=True — у каждого ролика свои параметры (случайно, в небольших пределах вокруг настроек).
"""
import math
import random as _random
import subprocess
from pathlib import Path

from .ffmpeg_path import ffmpeg_exe

DEFAULT_EFFECTS = {
    "zoom": 1.05,
    "rotate_deg": -0.5,
    "shadows": 0.10,
    "edge_blur": {"height": 0.10, "sigma": 18},
    "crf": 20,
    "strip_metadata": True,
    "random": True,        # свои параметры для каждого ролика
    "speed": True,         # случайная скорость 0.97–1.04 (только при random)
    "mirror": False,       # случайно зеркалить (портит надписи в кадре — по умолчанию выкл.)
    "subtitles": False,    # вшить субтитры из речи (whisper)
}


def randomize(effects, rng=None):
    """Копия эффектов со случайными вариациями вокруг настроек. Без random — как есть."""
    e = dict(effects)
    if not e.get("random", True):
        return e
    r = rng or _random.Random()
    base_zoom = float(e.get("zoom", 1.05))
    e["zoom"] = round(max(1.02, base_zoom + r.uniform(-0.02, 0.03)), 3)
    base_rot = abs(float(e.get("rotate_deg", 0.5))) or 0.5
    e["rotate_deg"] = round(r.choice((-1, 1)) * r.uniform(max(0.2, base_rot - 0.3), base_rot + 0.5), 2)
    e["shadows"] = round(max(0.0, float(e.get("shadows", 0.1)) + r.uniform(-0.04, 0.04)), 3)
    blur = dict(e.get("edge_blur") or {})
    if blur.get("height"):
        blur["height"] = round(max(0.05, blur["height"] + r.uniform(-0.02, 0.03)), 3)
        e["edge_blur"] = blur
    e["color"] = {"hue": round(r.uniform(-6, 6), 1), "saturation": round(r.uniform(0.95, 1.12), 3),
                  "brightness": round(r.uniform(-0.03, 0.03), 3), "contrast": round(r.uniform(0.97, 1.06), 3)}
    if e.get("speed", True):
        e["tempo"] = round(r.uniform(0.97, 1.04), 3)
    e["hflip"] = bool(e.get("mirror")) and r.random() < 0.5
    return e


def _even(expr):
    return f"trunc(({expr})/2)*2"


def build_filter(effects, subtitles=None):
    """subtitles — имя .ass-файла в рабочей папке ffmpeg (без пути: так надёжнее на Windows)."""
    zoom = float(effects.get("zoom", 1.0))
    rotate = float(effects.get("rotate_deg", 0.0))
    shadows = float(effects.get("shadows", 0.0))
    blur = effects.get("edge_blur") or {}
    band = float(blur.get("height", 0.0))
    sigma = float(blur.get("sigma", 0.0))

    # Поворот оставляет чёрные уголки — добираем зум, чтобы их срезать.
    # Для вертикального 9:16 при 0.5° хватает ~1.6%.
    angle = abs(math.radians(rotate))
    need = 1 + math.sin(angle) * 16 / 9 + (1 - math.cos(angle))
    zoom = max(zoom, need if rotate else 1.0)

    chain = []
    if zoom > 1.0:
        chain.append(f"scale={_even(f'iw*{zoom}')}:{_even(f'ih*{zoom}')}:flags=lanczos")
    if rotate:
        chain.append(f"rotate={math.radians(rotate):.6f}:ow=iw:oh=ih:c=black")
    if zoom > 1.0:
        chain.append(f"crop={_even(f'iw/{zoom}')}:{_even(f'ih/{zoom}')}")
    if shadows > 0:
        s = shadows
        # Поднимаем тёмную часть кривой, светлые тона почти не трогаем.
        pts = f"0/{s * 0.3:.4f} 0.25/{0.25 + s * 0.5:.4f} 0.5/{0.5 + s * 0.15:.4f} 1/1"
        chain.append(f"curves=all='{pts}'")
    color = effects.get("color") or {}
    if color:
        chain.append(f"eq=contrast={color.get('contrast', 1)}:brightness={color.get('brightness', 0)}"
                     f":saturation={color.get('saturation', 1)}")
        if color.get("hue"):
            chain.append(f"hue=h={color['hue']}")
    if effects.get("hflip"):
        chain.append("hflip")
    chain.append("setsar=1")
    graph = f"[0:v]{','.join(chain)}"

    # после размытия полос: субтитры (не размываются и не зеркалятся), потом скорость
    post = []
    if subtitles:
        post.append(f"ass={subtitles}")
    tempo = float(effects.get("tempo") or 1.0)
    if abs(tempo - 1) > 1e-3:
        post.append(f"setpts=PTS/{tempo}")
    post.append("format=yuv420p")

    if band > 0 and sigma > 0:
        graph += (
            "[base];[base]split=3[main][top][bot];"
            f"[top]crop=iw:{_even(f'ih*{band}')}:0:0,gblur=sigma={sigma}[tb];"
            f"[bot]crop=iw:{_even(f'ih*{band}')}:0:ih-{_even(f'ih*{band}')},gblur=sigma={sigma}[bb];"
            f"[main][tb]overlay=0:0[m1];[m1][bb]overlay=0:H-h,{','.join(post)}[v]"
        )
    else:
        graph += f",{','.join(post)}[v]"
    return graph


def shrink_to(path, max_mb, duration):
    """Пережимает видео, если оно больше max_mb (лимит Telegram для ботов — 50 МБ)."""
    import os

    path = str(path)
    if os.path.getsize(path) <= max_mb * 1024 * 1024 or not duration:
        return path
    total_kbps = max_mb * 8 * 1024 * 0.92 / duration
    video_kbps = max(int(total_kbps - 128), 300)
    tmp = path + ".small.mp4"
    subprocess.run([
        ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", path,
        "-c:v", "libx264", "-preset", "medium", "-b:v", f"{video_kbps}k",
        "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", tmp,
    ], check=True)
    os.replace(tmp, path)
    return path


def apply_effects(src, dst, effects, subtitles=None, progress=None):
    """subtitles — путь к .ass, который вшивается в видео. Возвращает dst.
    progress(доля 0..1) — по отчёту ffmpeg о готовой части."""
    src, dst = Path(src).resolve(), Path(dst).resolve()
    cwd = None
    if subtitles:
        subtitles = Path(subtitles).resolve()
        cwd = subtitles.parent
    tempo = float(effects.get("tempo") or 1.0)
    cmd = [
        ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-filter_complex", build_filter(effects, subtitles.name if subtitles else None),
        "-map", "[v]", "-map", "0:a?",
    ]
    if abs(tempo - 1) > 1e-3:
        cmd += ["-filter:a", f"atempo={tempo}"]
    cmd += [
        "-c:v", "libx264", "-preset", "medium",
        "-crf", str(effects.get("crf", 20)),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
    ]
    if effects.get("strip_metadata", True):
        cmd += ["-map_metadata", "-1", "-map_chapters", "-1"]
    cmd.append(str(dst))
    duration = None
    if progress:
        from .smartcut.media import probe

        try:
            duration = probe(src).duration / (tempo if tempo > 0 else 1)
        except Exception:  # noqa: BLE001 — без длительности просто без процентов
            duration = None
    from . import ffprog

    ffprog.run(cmd, duration, progress, cwd=cwd)
    return dst
