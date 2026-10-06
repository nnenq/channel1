import functools
import os
import shutil
import subprocess


def ffmpeg_exe():
    """Системный ffmpeg, а если его нет — бинарник из пакета imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


@functools.lru_cache(maxsize=1)
def nvenc_available():
    """Есть ли видеокарта NVIDIA, на которой ffmpeg умеет кодировать (h264_nvenc). Проверяется один раз."""
    if os.getenv("VIDEO_ENCODER", "auto").lower() == "cpu":
        return False
    try:
        r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                            "color=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def video_args(crf=21, intermediate=False):
    """Аргументы кодирования видео.

    Видеокарта NVIDIA (если есть) кодирует в разы быстрее процессора. На процессоре — пресет
    veryfast: в ~2 раза быстрее medium при том же качестве (CRF), файл почти того же размера.
    intermediate=True — промежуточный файл, который потом всё равно перекодируется: максимально
    быстро и почти без потерь. VIDEO_ENCODER=cpu в .env — не трогать видеокарту, VIDEO_PRESET —
    свой пресет x264 (medium — медленнее и чуть компактнее)."""
    if nvenc_available():
        return ["-c:v", "h264_nvenc", "-preset", "p2" if intermediate else "p5", "-rc", "vbr",
                "-cq", str(16 if intermediate else crf + 2), "-b:v", "0"]
    if intermediate:
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "14"]
    return ["-c:v", "libx264", "-preset", os.getenv("VIDEO_PRESET", "veryfast"), "-crf", str(crf)]
