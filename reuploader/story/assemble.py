"""Сборка ролика: голос автора + кадры мультфильма под каждую фразу + анимированные субтитры.

1. Голос распознаётся (whisper) — получаем слова с таймкодами.
2. align(): каждая фраза сценария получает свой отрезок в записи голоса
   (сопоставление слов сценария и распознанных слов; если человек отошёл от текста —
   пропорционально числу слов).
3. plan_clips(): под каждую фразу — кусок мультфильма той же длины, начиная с отрезка,
   который указан в сценарии.
4. render(): вертикальное видео 1080×1920 — кадр мультфильма по центру, размытый фон,
   надпись-крючок сверху, субтитры по голосу. Звук мультфильма выключен (по умолчанию).
"""
import difflib
import re
import subprocess
from pathlib import Path

from ..ffmpeg_path import ffmpeg_exe
from ..smartcut.media import probe

W, H = 1080, 1920
_WORD = re.compile(r"[a-zа-яё0-9']+", re.I)


def _norm_tokens(s):
    return _WORD.findall((s or "").lower().replace("ё", "е"))


def align(lines, words, voice_dur):
    """-> [(t0, t1)] — где в записи голоса звучит каждая фраза сценария."""
    n = len(lines)
    if not n:
        return []
    script, owner = [], []
    for i, ln in enumerate(lines):
        toks = _norm_tokens(ln["text"]) or ["_"]
        script += toks
        owner += [i] * len(toks)
    voice = [(_norm_tokens(w.text) or ["_"])[0] for w in words]
    starts = [None] * n
    if voice:
        sm = difflib.SequenceMatcher(a=script, b=voice, autojunk=False)
        for a, b, size in sm.get_matching_blocks():
            for k in range(size):
                i = owner[a + k]
                if starts[i] is None:
                    starts[i] = words[b + k].start
    # фразы без совпадений — пропорционально словам между соседями
    counts = [max(1, len(_norm_tokens(ln["text"]))) for ln in lines]
    total = sum(counts)
    cum = 0
    for i in range(n):
        if starts[i] is None:
            starts[i] = voice_dur * cum / total
        cum += counts[i]
    starts[0] = 0.0
    for i in range(1, n):                     # время идёт только вперёд
        starts[i] = max(starts[i], starts[i - 1] + 0.3)
    starts = [min(s, max(0.0, voice_dur - 0.3 * (n - i))) for i, s in enumerate(starts)]
    ends = starts[1:] + [voice_dur]
    return [(round(a, 3), round(max(b, a + 0.3), 3)) for a, b in zip(starts, ends)]


def plan_clips(lines, spans, src_dur):
    """-> [(начало в мультфильме, длительность)] — по куску на каждую фразу."""
    clips = []
    for ln, (t0, t1) in zip(lines, spans):
        d = t1 - t0
        start = max(0.0, min(float(ln["from"]), max(0.0, src_dur - d - 0.05)))
        clips.append((round(start, 3), round(d, 3)))
    return clips


def build_filter(n, subs_name=None):
    """Склейка n кусков (входы 0..n-1) и вертикальная компоновка."""
    parts = "".join(f"[{i}:v]setpts=PTS-STARTPTS,fps=30,scale=1280:-2,setsar=1[c{i}];" for i in range(n))
    cat = "".join(f"[c{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[cat];"
    # кадр по центру: 4:3 из середины (как в популярных Shorts), фон — тот же кадр, размытый
    layout = ("[cat]split[fg][bg];"
              "[fg]crop='min(iw,ih*4/3)':ih,scale=1080:-2[f];"
              f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=40,"
              "eq=brightness=-0.08[b];"
              "[b][f]overlay=0:(H-h)/2")
    tail = (f",ass={subs_name}" if subs_name else "") + ",format=yuv420p[v]"
    return parts + cat + layout + tail


def render(src, voice, clips, out, subs=None, crf=21, progress=None):
    """Собирает ролик. clips — [(start, dur)]; voice — запись голоса; subs — .ass (или None)."""
    src, voice, out = str(Path(src).resolve()), str(Path(voice).resolve()), Path(out).resolve()
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    for start, dur in clips:
        cmd += ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src]
    cmd += ["-i", voice]
    cwd = None
    subs_name = None
    if subs:
        subs = Path(subs).resolve()
        cwd, subs_name = subs.parent, subs.name
    n = len(clips)
    fc = build_filter(n, subs_name) + f";[{n}:a]loudnorm=I=-15:TP=-1.5:LRA=11,aresample=48000[a]"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart",
            "-map_metadata", "-1", str(out)]
    from .. import ffprog

    ffprog.run(cmd, sum(d for _, d in clips), progress, cwd=cwd)
    return out


def build_story(src, voice, script, out, transcriber, work_dir, progress=None):
    """Всё вместе. -> dict(duration, lines, words) для отчёта.
    progress(этап, доля 0..1) — распознавание голоса 0–35 %, сборка видео 35–100 %."""
    from ..subtitles import to_ass

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    vinfo = probe(voice)
    if vinfo.duration < 3:
        raise ValueError("Запись голоса слишком короткая (меньше 3 секунд).")
    if progress:
        progress("распознаю голос", 0.0)
    words = transcriber(voice)
    lines = script["lines"]
    spans = align(lines, words, vinfo.duration)
    clips = plan_clips(lines, spans, probe(src).duration)
    subs = to_ass(words, W, H, work / "story.ass", overlay=script.get("overlay") or None,
                  overlay_until=vinfo.duration)
    render(src, voice, clips, out, subs,
           progress=(lambda f: progress("собираю видео", 0.35 + 0.65 * f)) if progress else None)
    return {"duration": round(vinfo.duration, 1), "lines": len(lines), "words": len(words),
            "spans": spans, "clips": clips}
