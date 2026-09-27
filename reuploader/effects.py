"""Лёгкая уникализация видео через ffmpeg.

Цепочка: увеличение -> поворот -> обрезка до исходного размера ->
осветление теней -> размытие полосок сверху и снизу.
"""
import math
import subprocess

from .ffmpeg_path import ffmpeg_exe


def _even(expr):
    return f"trunc(({expr})/2)*2"


def build_filter(effects):
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
    chain.append("setsar=1")
    graph = f"[0:v]{','.join(chain)}"

    if band > 0 and sigma > 0:
        graph += (
            "[base];[base]split=3[main][top][bot];"
            f"[top]crop=iw:{_even(f'ih*{band}')}:0:0,gblur=sigma={sigma}[tb];"
            f"[bot]crop=iw:{_even(f'ih*{band}')}:0:ih-{_even(f'ih*{band}')},gblur=sigma={sigma}[bb];"
            "[main][tb]overlay=0:0[m1];[m1][bb]overlay=0:H-h,format=yuv420p[v]"
        )
    else:
        graph += ",format=yuv420p[v]"
    return graph


def apply_effects(src, dst, effects):
    cmd = [
        ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-filter_complex", build_filter(effects),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium",
        "-crf", str(effects.get("crf", 20)),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
    ]
    if effects.get("strip_metadata", True):
        cmd += ["-map_metadata", "-1", "-map_chapters", "-1"]
    cmd.append(str(dst))
    subprocess.run(cmd, check=True)
    return dst
