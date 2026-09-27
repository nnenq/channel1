import shutil


def ffmpeg_exe():
    """Системный ffmpeg, а если его нет — бинарник из пакета imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()
