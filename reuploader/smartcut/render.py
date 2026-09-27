"""Сборка результата: trim/atrim + concat, аудио-кроссфейды на стыках, loudnorm в два прохода."""
import json
import re

from .media import run_ffmpeg

XFADE = 0.05   # длина аудио-кроссфейда на стыке, сек (30–80 мс)


def _graph(segments, has_audio, duration, loudnorm=None):
    """segments: [(start, end)]. Аудио каждого куска (кроме последнего) берём на XFADE
    длиннее — это захватывает тишину паузы и компенсирует укорачивание от acrossfade,
    так что звук и картинка остаются синхронными."""
    parts = []
    n = len(segments)
    for i, (s, e) in enumerate(segments):
        parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        if has_audio:
            ae = min(e + (XFADE if i < n - 1 else 0), duration)
            parts.append(f"[0:a]atrim=start={s:.3f}:end={ae:.3f},asetpts=PTS-STARTPTS[a{i}]")
    parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0,format=yuv420p[vout]")
    if has_audio:
        prev = "a0"
        for i in range(1, n):
            out = f"x{i}"
            parts.append(f"[{prev}][a{i}]acrossfade=d={XFADE}:c1=tri:c2=tri[{out}]")
            prev = out
        norm = "loudnorm=I=-14:TP=-1.5:LRA=11"
        if loudnorm:
            norm += (":measured_I={input_i}:measured_TP={input_tp}:measured_LRA={input_lra}"
                     ":measured_thresh={input_thresh}:offset={target_offset}:linear=true").format(**loudnorm)
        parts.append(f"[{prev}]{norm},aresample=48000[aout]")
    return ";".join(parts)


def measure_loudness(src, segments, duration):
    graph = _graph(segments, True, duration).replace(
        "loudnorm=I=-14:TP=-1.5:LRA=11", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json")
    graph = re.sub(r"\[0:v\][^;]*;", "", graph)            # для замера картинка не нужна
    graph = re.sub(r";[^;]*concat=[^;]*\[vout\]", "", graph)
    err = run_ffmpeg(["-i", str(src), "-filter_complex", graph, "-map", "[aout]", "-f", "null", "-"])
    m = re.search(r"\{\s*\"input_i\"[^}]*\}", err)
    return json.loads(m.group(0)) if m else None


def render(src, dst, segments, info, crf=18):
    has_audio = info.audio_streams > 0
    measured = measure_loudness(src, segments, info.duration) if has_audio else None
    graph = _graph(segments, has_audio, info.duration, measured)
    args = ["-y", "-i", str(src), "-filter_complex", graph, "-map", "[vout]"]
    if has_audio:
        args += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-r", f"{info.fps:.3f}",
             "-movflags", "+faststart", "-map_metadata", "-1", str(dst)]
    run_ffmpeg(args)
    return dst
