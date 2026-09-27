"""Информация о файле и декодирование звука — через ffmpeg (ffprobe не требуется)."""
import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..ffmpeg_path import ffmpeg_exe

SR = 16000   # частота для анализа звука


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    audio_streams: int
    audio_rate: int


def run_ffmpeg(args, check=True):
    """Запускает ffmpeg, возвращает stderr (там ffmpeg пишет всю диагностику)."""
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", *args],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError("ffmpeg: " + p.stderr.strip().splitlines()[-1] if p.stderr else "ffmpeg failed")
    return p.stderr


def probe(path):
    err = run_ffmpeg(["-i", str(path)], check=False)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not m:
        raise RuntimeError(f"Не удалось прочитать видео: {Path(path).name}")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    width = height = 0
    fps = 30.0
    v = re.search(r"Stream #[^\n]*Video:[^\n]*?(\d{2,5})x(\d{2,5})[^\n]*", err)
    if v:
        width, height = int(v.group(1)), int(v.group(2))
        f = re.search(r"([\d.]+) fps", v.group(0)) or re.search(r"([\d.]+) tbr", v.group(0))
        if f:
            fps = float(f.group(1))
    audio = re.findall(r"Stream #[^\n]*Audio:[^\n]*", err)
    rate = 48000
    if audio:
        r = re.search(r"(\d{4,6}) Hz", audio[0])
        rate = int(r.group(1)) if r else rate
    return MediaInfo(duration, width, height, fps, len(audio), rate)


def load_audio(path, work_dir, sr=SR):
    """Моно float32 [-1, 1] с частотой sr. Пустой массив, если звука нет."""
    wav = Path(work_dir) / f"_analysis_{Path(path).stem}.wav"
    try:
        run_ffmpeg(["-y", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(wav)])
    except RuntimeError:
        return np.zeros(0, dtype=np.float32)
    with wave.open(str(wav), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    wav.unlink(missing_ok=True)
    return data.astype(np.float32) / 32768.0
