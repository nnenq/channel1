"""Запуск ffmpeg с прогрессом: ffmpeg сам пишет, до какой секунды дошёл (-progress pipe:1)."""
import subprocess
import threading


def run(cmd, duration=None, progress=None, cwd=None):
    """Как subprocess.run(cmd, check=True), но с progress(доля 0..1), если известна длительность."""
    if not progress or not duration:
        subprocess.run(cmd, check=True, cwd=cwd, capture_output=True)
        return
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats"] + list(cmd[1:])
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd,
                         encoding="utf-8", errors="replace")
    err = []
    t = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)   # чтобы stderr не забил трубу
    t.start()
    for line in p.stdout:
        key, _, val = line.strip().partition("=")
        if key in ("out_time_us", "out_time_ms") and val.isdigit():
            progress(max(0.0, min(1.0, int(val) / 1e6 / duration)))
    rc = p.wait()
    t.join(timeout=5)
    if rc:
        raise subprocess.CalledProcessError(rc, cmd, stderr="".join(err))
    progress(1.0)
