"""Встроенные фоновые мелодии: бот синтезирует их сам (numpy), поэтому на них нет ни у кого
прав и Content ID их не находит. Нужны, чтобы музыка была даже без своих треков.

PRESETS: fun — лёгкая мажорная (пересказы), mystery — загадочная минорная (теории).
Мелодия — аккорды-подложка, мягкое арпеджио, бас и тихие ударные; длина кратна такту,
поэтому при зацикливании стыка не слышно. Готовый файл кэшируется в data/music_builtin/.
"""
import subprocess
import wave
from pathlib import Path

import numpy as np

SR = 32000
PRESETS = {
    "fun": {"title": "Встроенная: весёлая", "bpm": 100,
            # C - G - Am - F (по 2 такта)
            "chords": [(48, (0, 4, 7)), (43, (0, 4, 7, 12)), (45, (0, 3, 7)), (41, (0, 4, 7))]},
    "mystery": {"title": "Встроенная: загадочная", "bpm": 84,
                # Am - F - Dm - E
                "chords": [(45, (0, 3, 7)), (41, (0, 4, 7)), (38, (0, 3, 7)), (40, (0, 4, 7))]},
}
ROUNDS = 4          # сколько раз повторить последовательность (≈ 1,5–2 мин)


def _hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def _tone(freq, dur, harmonics=(1.0, 0.35, 0.12), detune=0.0):
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    for k, amp in enumerate(harmonics, 1):
        out += amp * np.sin(2 * np.pi * freq * k * (1 + detune) * t)
    return out


def _env(n, attack, release):
    e = np.ones(n)
    a, r = min(n, int(attack * SR)), min(n, int(release * SR))
    if a:
        e[:a] = np.linspace(0, 1, a)
    if r:
        e[n - r:] *= np.linspace(1, 0, r)
    return e


def synth(preset="fun", seed=7):
    p = PRESETS[preset]
    beat = 60 / p["bpm"]
    bar = 4 * beat
    chord_len = 2 * bar
    total = chord_len * len(p["chords"]) * ROUNDS
    mix = np.zeros(int(total * SR) + SR)
    rng = np.random.default_rng(seed)

    def add(sig, at):
        i = int(at * SR)
        mix[i:i + len(sig)] += sig[: len(mix) - i]

    for r in range(ROUNDS):
        for c, (root, ivs) in enumerate(p["chords"]):
            t0 = (r * len(p["chords"]) + c) * chord_len
            # подложка: аккорд, медленная атака, лёгкий «хорус»
            for iv in ivs:
                f = _hz(root + 12 + iv)
                pad = (_tone(f, chord_len + 0.4, (1, 0.3, 0.08)) +
                       _tone(f, chord_len + 0.4, (1, 0.3, 0.08), detune=0.003))
                add(0.035 * pad * _env(len(pad), 0.6, 0.6), t0)
            # бас на 1 и 3 долю
            for b in range(8):
                if b % 2 == 0:
                    n = int(beat * 1.6 * SR)
                    bass = _tone(_hz(root), beat * 1.6, (1, 0.25)) * np.exp(-np.arange(n) / SR * 2.2)
                    add(0.16 * bass * _env(n, 0.01, 0.12), t0 + b * beat)          # без щелчков
            # арпеджио восьмыми
            notes = [root + 24 + iv for iv in ivs] + [root + 36]
            for k in range(16):
                f = _hz(notes[(k * 2 + (k // 4)) % len(notes)])
                n = int(beat * 0.9 * SR)
                pl = _tone(f, beat * 0.9, (1, 0.2, 0.05)) * np.exp(-np.arange(n) / SR * 6) * _env(n, 0.006, 0.05)
                add(0.05 * pl * (0.8 + 0.2 * rng.random()), t0 + k * beat / 2)
            # тихие ударные: бочка на 1 и 3, хэт на слабые доли
            for b in range(8):
                at = t0 + b * beat
                if b % 2 == 0:
                    n = int(0.18 * SR)
                    tt = np.arange(n) / SR
                    kick = np.sin(2 * np.pi * (45 + 75 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 18) * _env(n, 0.003, 0.03)
                    add(0.22 * kick, at)
                n = int(0.04 * SR)
                hat = rng.standard_normal(n) * np.exp(-np.arange(n) / SR * 120) * _env(n, 0.002, 0.01)
                hat = np.convolve(np.diff(hat, prepend=0), np.ones(3) / 3, "same")   # мягкий «шорох»
                add(0.012 * hat, at + beat / 2)
    # мягкое эхо и нормализация; хвост заворачиваем в начало — цикл без стыка
    d = int(beat * 0.75 * SR)
    mix[d:] += 0.25 * mix[:-d]
    n = int(total * SR)
    tail = mix[n:]
    mix = mix[:n]
    mix[: len(tail)] += tail
    mix *= 0.89 / max(1e-9, np.abs(mix).max())
    return mix


def ensure(folder, preset="fun"):
    """Готовый mp3 встроенной мелодии (кэш). -> Path."""
    from .ffmpeg_path import ffmpeg_exe

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{preset}.mp3"
    if out.exists() and out.stat().st_size > 10000:
        return out
    wav = folder / f"{preset}.wav"
    pcm = (synth(preset) * 32767).astype(np.int16)
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "128k",
                    str(out)], check=True)
    wav.unlink(missing_ok=True)
    return out
