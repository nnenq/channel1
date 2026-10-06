"""Разметка ролика: слова с таймкодами, паузы, смены сцен, громкость по времени."""
import logging
import os
import re
from dataclasses import dataclass, field

import numpy as np

from .media import SR, run_ffmpeg

log = logging.getLogger("smartcut")
HOP = 0.05   # шаг кривой громкости, сек


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Analysis:
    duration: float
    words: list = field(default_factory=list)       # [Word]
    silences: list = field(default_factory=list)    # [(start, end)]
    scenes: list = field(default_factory=list)      # [время смены кадра]
    rms_db: np.ndarray = None                       # громкость каждые HOP сек
    noise_db: float = -40.0

    def loudness(self, t0, t1):
        a, b = int(t0 / HOP), max(int(t0 / HOP) + 1, int(t1 / HOP))
        seg = self.rms_db[a:b]
        return seg if len(seg) else np.array([-90.0])


def rms_curve(samples, sr=SR, hop=HOP):
    n = int(sr * hop)
    if len(samples) < n:
        return np.array([-90.0])
    frames = samples[: len(samples) // n * n].reshape(-1, n)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
    return 20 * np.log10(rms)


def noise_floor(rms_db):
    """Порог тишины: чуть выше самых тихих участков, в разумных пределах."""
    return float(np.clip(np.percentile(rms_db, 10) + 10, -55, -28))


def detect_silences(path, noise_db, min_dur=0.2):
    err = run_ffmpeg(["-i", str(path), "-vn", "-af", f"silencedetect=noise={noise_db:.1f}dB:d={min_dur}",
                      "-f", "null", "-"], check=False)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        out.append((max(0.0, s), e))
    return out


def detect_scenes(path, threshold=0.25):
    """Смены кадра: PySceneDetect, если установлен, иначе фильтр scene в ffmpeg."""
    try:
        from scenedetect import ContentDetector, detect

        return [s[0].get_seconds() for s in detect(str(path), ContentDetector())][1:]
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("PySceneDetect: %s — использую ffmpeg", e)
    err = run_ffmpeg(["-i", str(path), "-an", "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo",
                      "-f", "null", "-"], check=False)
    return [float(x) for x in re.findall(r"pts_time:([\d.]+)", err)]


_MODELS = {}
_LOCK = __import__("threading").Lock()       # одна общая модель — распознаём по очереди


def _whisper_model(model_size):
    """Модель whisper загружается один раз на процесс (раньше — на каждый ролик, плюс запрос к
    huggingface.co). Уже скачанная модель берётся с диска без интернета. Видеокарта NVIDIA (CUDA)
    используется сама, если есть."""
    from faster_whisper import WhisperModel

    if model_size not in _MODELS:
        try:
            _MODELS[model_size] = WhisperModel(model_size, device="auto", compute_type="int8", local_files_only=True)
        except Exception:  # noqa: BLE001 — модели ещё нет на диске: скачиваем
            _MODELS[model_size] = WhisperModel(model_size, device="auto", compute_type="int8")
    return _MODELS[model_size]


def whisper_transcribe(path, model_size="small", language=None, progress=None):
    """Слова с таймкодами через faster-whisper (локально, без сети после загрузки модели).
    progress(доля 0..1) — по тому, до какой секунды файла дошло распознавание."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning("faster-whisper не установлен — режу только по паузам, без учёта речи")
        return []
    with _LOCK:
        model = _whisper_model(model_size)
        segments, info = model.transcribe(str(path), word_timestamps=True, language=language, vad_filter=True,
                                          beam_size=int(os.getenv("WHISPER_BEAM", "1")))
        duration = getattr(info, "duration", 0) or 0
        words = []
        for seg in segments:
            if progress and duration:
                progress(min(1.0, float(seg.end) / duration))
            for w in seg.words or []:
                words.append(Word(float(w.start), float(w.end), w.word.strip()))
    return words


def analyze(path, info, samples, transcriber=None, progress=lambda *_: None):
    a = Analysis(duration=info.duration)
    a.rms_db = rms_curve(samples) if len(samples) else np.full(int(info.duration / HOP) + 1, -90.0)
    a.noise_db = noise_floor(a.rms_db)
    progress("паузы", 0.15)
    if info.audio_streams:
        a.silences = [(s, e if e is not None else info.duration)
                      for s, e in detect_silences(path, a.noise_db)]
    progress("смены сцен", 0.25)
    a.scenes = detect_scenes(path)
    progress("речь", 0.35)
    if info.audio_streams:
        a.words = (transcriber or whisper_transcribe)(path)
    return a
