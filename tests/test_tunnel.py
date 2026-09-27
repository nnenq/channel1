import asyncio
import os
import stat
import sys
import time

import pytest

from reuploader.bot import tunnel

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="фейковый cloudflared — shell-скрипт")


@pytest.fixture
def fake_cloudflared(tmp_path):
    """Печатает в stderr новый адрес (как cloudflared) и живёт, пока его не убьют."""
    exe = tmp_path / "cloudflared"
    exe.write_text(f"""#!{sys.executable}
import sys, time, random
sys.stderr.write("INF |  https://fake-%d-tunnel.trycloudflare.com  |\\n" % random.randint(1, 10**9))
sys.stderr.flush()
time.sleep(3600)
""")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def test_tunnel_survives_bot_restart_and_recovers(fake_cloudflared, tmp_path):
    async def ok(url):
        return True

    async def dead(url):
        return False

    async def scenario():
        url1, reused1 = await tunnel.ensure_tunnel(fake_cloudflared, "http://localhost:1", tmp_path, check=ok)
        pid1 = __import__("json").loads((tmp_path / "tunnel.json").read_text())["pid"]
        # «перезапуск бота»: туннель жив -> тот же адрес
        url2, reused2 = await tunnel.ensure_tunnel(fake_cloudflared, "http://localhost:1", tmp_path, check=ok)
        # туннель есть, но не отвечает -> старый убит, поднят новый
        url3, reused3 = await tunnel.ensure_tunnel(fake_cloudflared, "http://localhost:1", tmp_path, check=dead)
        time.sleep(0.3)
        alive_old = tunnel.pid_alive(pid1)
        # «перезагрузка компьютера»: процесс умер -> новый адрес
        pid3 = __import__("json").loads((tmp_path / "tunnel.json").read_text())["pid"]
        os.kill(pid3, 9)
        os.waitpid(pid3, 0) if False else None
        time.sleep(0.3)
        url4, reused4 = await tunnel.ensure_tunnel(fake_cloudflared, "http://localhost:1", tmp_path, check=ok)
        tunnel.kill(__import__("json").loads((tmp_path / "tunnel.json").read_text())["pid"])
        return url1, reused1, url2, reused2, url3, reused3, alive_old, url4, reused4

    url1, r1, url2, r2, url3, r3, alive_old, url4, r4 = asyncio.run(scenario())
    assert url1.endswith(".trycloudflare.com") and not r1
    assert url2 == url1 and r2
    assert url3 != url1 and not r3 and not alive_old
    assert url4 not in (url1, url3) and not r4


def test_no_url_when_cloudflared_fails(tmp_path):
    exe = tmp_path / "cloudflared"
    exe.write_text(f"#!{sys.executable}\nimport sys; sys.exit(1)\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    url, reused = asyncio.run(tunnel.ensure_tunnel(str(exe), "http://localhost:1", tmp_path, wait=3))
    assert url is None and not reused
