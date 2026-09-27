"""Бесплатный HTTPS-адрес через Cloudflare Quick Tunnel, который переживает перезапуск бота.

cloudflared запускается отдельным процессом и НЕ закрывается вместе с ботом. Его pid и адрес
лежат в data/tunnel.json: при следующем запуске бот проверяет, что туннель жив и ведёт к нему
(GET /healthz), и берёт тот же адрес. Новый адрес появляется только если туннель умер
(например, после перезагрузки компьютера).
"""
import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

log = logging.getLogger("tunnel")
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
HEALTH_TEXT = "shortsbot-ok"


def pid_alive(pid):
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259                                                # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    try:     # завершившийся, но не убранный процесс («зомби») — не живой
        with open(f"/proc/{int(pid)}/status", encoding="ascii", errors="ignore") as f:
            return not any(line.startswith("State:") and "Z" in line.split()[1] for line in f)
    except OSError:
        return True


def kill(pid):
    if not pid_alive(pid):
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(int(pid), signal.SIGTERM)
            for _ in range(20):          # если это наш дочерний процесс — забираем его
                try:
                    if os.waitpid(int(pid), os.WNOHANG)[0]:
                        break
                except ChildProcessError:
                    break
                time.sleep(0.05)
    except OSError:
        pass


async def healthy(url, timeout=12):
    """Туннель жив и ведёт именно к этому боту."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url + "/healthz", timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return r.status == 200 and (await r.text()).strip() == HEALTH_TEXT
    except Exception:  # noqa: BLE001
        return False


def _spawn(exe, local_url, log_path):
    logf = open(log_path, "w", encoding="utf-8", errors="replace")   # noqa: SIM115 — держит cloudflared
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": logf, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", local_url], **kwargs)


async def ensure_tunnel(exe, local_url, data_dir, check=healthy, wait=60):
    """-> (url, reused). url=None, если поднять туннель не удалось."""
    state_path = Path(data_dir) / "tunnel.json"
    log_path = Path(data_dir) / "cloudflared.log"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    if state.get("url") and pid_alive(state.get("pid")):
        if await check(state["url"]):
            log.info("туннель жив — адрес прежний")
            return state["url"], True
        kill(state.get("pid"))       # процесс есть, но туннель не работает — перезапускаем

    proc = _spawn(exe, local_url, log_path)
    deadline = time.monotonic() + wait
    url = None
    while time.monotonic() < deadline and proc.poll() is None:
        await asyncio.sleep(0.5)
        try:
            m = TUNNEL_RE.search(log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            m = None
        if m:
            url = m.group(0)
            break
    if not url:
        kill(proc.pid)
        return None, False
    state_path.write_text(json.dumps({"pid": proc.pid, "url": url}), encoding="utf-8")
    return url, False
