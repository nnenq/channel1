"""Синтетические тестовые видео: цветные сцены + тон («речь») + паузы (тишина)."""
import subprocess

from reuploader.ffmpeg_path import ffmpeg_exe
from reuploader.smartcut.analyze import Word

COLORS = {"red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
          "magenta": (255, 0, 255), "cyan": (0, 255, 255), "orange": (255, 128, 0), "purple": (128, 0, 255),
          "white": (255, 255, 255), "gray": (128, 128, 128)}


def make_video(path, scenes, size="360x640", fps=30, music=False):
    """scenes: [(color, [(speech_sec, pause_sec, text), ...])]. Возвращает (длина, паузы, слова, границы сцен)."""
    v_inputs, a_inputs, pauses, words, scene_starts = [], [], [], [], []
    t = 0.0
    freq = 300
    for color, phrases in scenes:
        scene_starts.append(t)
        scene_len = sum(s + p for s, p, _ in phrases)
        v_inputs.append(f"color=c={color}:s={size}:r={fps}:d={scene_len:.3f}")
        for speech, pause, text in phrases:
            a_inputs.append(f"sine=f={freq}:sample_rate=48000:d={speech:.3f}")
            toks = text.split()
            step = speech / len(toks)
            for k, tok in enumerate(toks):
                words.append(Word(t + k * step + 0.02, t + (k + 1) * step - 0.03, tok))
            t += speech
            if pause:
                a_inputs.append(f"anullsrc=r=48000:cl=mono:d={pause:.3f}")
                pauses.append((t, t + pause))
                t += pause
            freq += 40
    args = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"]
    for s in v_inputs + a_inputs:
        args += ["-f", "lavfi", "-i", s]
    nv, na = len(v_inputs), len(a_inputs)
    graph = "".join(f"[{i}:v]" for i in range(nv)) + f"concat=n={nv}:v=1:a=0[v];"
    graph += "".join(f"[{nv + i}:a]" for i in range(na)) + f"concat=n={na}:v=0:a=1,volume=0.5[a0]"
    if music:   # тихая «фоновая музыка» под всем роликом — паузы уже не тишина
        args += ["-f", "lavfi", "-i", f"sine=f=110:sample_rate=48000:d={t:.3f}"]
        graph += f";[{nv + na}:a]volume=0.08[m];[a0][m]amix=inputs=2:normalize=0[a]"
    else:
        graph += ";[a0]anull[a]"
    args += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)]
    subprocess.run(args, check=True)
    return t, pauses, words, scene_starts


def frame_color(path, t):
    out = subprocess.run([ffmpeg_exe(), "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    rgb = tuple(out[:3])
    return min(COLORS, key=lambda c: sum((a - b) ** 2 for a, b in zip(COLORS[c], rgb)))
