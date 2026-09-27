"""Автопроверка результата: длина, щелчки и провалы громкости на стыках, чёрные кадры."""
import re

import numpy as np

from .media import SR, load_audio, probe, run_ffmpeg

WIN = 0.06   # окно вокруг стыка, сек


def _rms_db(x):
    return 20 * np.log10(np.sqrt(np.mean(x ** 2) + 1e-12)) if len(x) else -90.0


def check(dst, joints, target, tolerance, work_dir):
    """joints — моменты стыков в РЕЗУЛЬТАТЕ (сек). Возвращает (problems, stats)."""
    info = probe(dst)
    problems = []
    lo, hi = target * (1 - tolerance), target * (1 + tolerance)
    if target and not lo - 0.05 <= info.duration <= hi + 0.05:
        problems.append((None, f"длина {info.duration:.1f} с вне допуска {lo:.1f}–{hi:.1f} с"))

    clicks = dips = 0
    if info.audio_streams:
        a = load_audio(dst, work_dir)
        d = np.abs(np.diff(a)) if len(a) > 1 else np.zeros(0)
        for k, t in enumerate(joints):
            c0, c1 = int((t - WIN) * SR), int((t + WIN) * SR)
            if c0 < SR // 2 or c1 > len(a) - SR // 2:
                continue
            ctx = np.concatenate([d[c0 - SR // 2:c0], d[c1:c1 + SR // 2]])
            base = float(np.percentile(ctx, 99)) if len(ctx) else 0.0
            if d[c0:c1].max() > max(3 * base, 0.02):
                problems.append((k, f"щелчок на стыке {k + 1} ({t:.2f} с)"))
                clicks += 1
            before = _rms_db(a[c0 - int(0.3 * SR):c0])
            after = _rms_db(a[c1:c1 + int(0.3 * SR)])
            here = _rms_db(a[c0:c1])
            if min(before, after) > -40 and here < min(before, after) - 18:
                problems.append((k, f"провал громкости на стыке {k + 1} ({t:.2f} с)"))
                dips += 1

    err = run_ffmpeg(["-i", str(dst), "-an", "-vf", "scale=160:-2,blackdetect=d=0.08:pix_th=0.10",
                      "-f", "null", "-"], check=False)
    blacks = [(float(s), float(e)) for s, e in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)]
    black_joints = 0
    for k, t in enumerate(joints):
        if any(s - 0.2 <= t <= e + 0.2 for s, e in blacks):
            problems.append((k, f"чёрные кадры на стыке {k + 1} ({t:.2f} с)"))
            black_joints += 1
    return problems, {"duration": round(info.duration, 2), "clicks": clicks, "dips": dips,
                      "black_joints": black_joints, "black_total": len(blacks)}
