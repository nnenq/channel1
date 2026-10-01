"""Замена вшитых субтитров: поиск полосы со старым текстом, план (обрезать/размыть), вся задача в очереди."""
import asyncio
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from reuploader import resub
from reuploader.ffmpeg_path import ffmpeg_exe
from reuploader.smartcut.analyze import Word
from reuploader.smartcut.media import probe
from reuploader.subtitles import to_ass

W, H = 540, 720


@pytest.fixture(scope="module")
def captioned(tmp_path_factory):
    """Мультяшное видео 540×720 с вшитыми субтитрами (белый текст с чёрной обводкой) внизу кадра."""
    d = tmp_path_factory.mktemp("resub")
    words = [Word(i * 0.5, i * 0.5 + 0.45, w) for i, w in
             enumerate("SPONGEBOB OPENED THE FREEZER AND FOUND SAND INSIDE EVERY PATTY TODAY".split() * 2)]
    ass = to_ass(words, W, H, d / "old.ass", animated=False)
    out = d / "captioned.mp4"
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", f"life=s={W}x{H}:r=25:ratio=0.4:mold=8:life_color=#3aa0ff:death_color=#204020:mold_color=#c06030",
                    "-f", "lavfi", "-i", "sine=f=300:sample_rate=44100", "-t", "12",
                    "-vf", f"ass={ass.name}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(out)], check=True, cwd=d)
    return out


def test_finds_old_caption_band_and_crops_it(captioned):
    lay = resub.analyze(captioned)
    assert lay.width == W and lay.height == H
    assert len(lay.bands) == 1
    a, b = lay.bands[0]
    cap_y = H - round(H * 0.28)                    # где to_ass ставит субтитры
    assert a < cap_y and b > cap_y - 40            # полоса — там, где были старые субтитры
    assert lay.mode == "crop" and lay.keep[1] <= a + 2 and lay.keep[1] - lay.keep[0] >= 0.6 * H


def test_plan_blurs_when_text_is_in_the_middle():
    lay = resub.plan(resub.Layout(1080, 1920, (300, 1500), [(700, 900), (1100, 1200)]))
    assert lay.mode == "blur" and lay.keep == (300, 1500) and lay.blur == [(700, 900), (1100, 1200)]
    clean = resub.plan(resub.Layout(1080, 1920, (300, 1500), []))
    assert clean.mode == "clean" and clean.keep == (300, 1500)
    assert "boxblur" in resub.build_filter(lay) and "boxblur" not in resub.build_filter(clean)


def test_replace_subtitles_renders_vertical_with_ours(captioned, tmp_path):
    heard = [Word(0.2, 0.6, "Губка"), Word(0.7, 1.2, "Боб"), Word(1.3, 2.0, "открыл"), Word(2.1, 2.8, "морозилку.")]
    out = tmp_path / "out.mp4"
    stages = []
    rep = resub.replace_subtitles(captioned, out, lambda p: heard, tmp_path / "w",
                                  progress=lambda st, f: stages.append((st, f)), method="crop")
    info = probe(out)
    assert (info.width, info.height) == (1080, 1920) and abs(info.duration - 12) < 0.5
    assert rep["mode"] == "crop" and rep["words"] == 4
    assert "ГУБКА" in (tmp_path / "w" / "subs.ass").read_text(encoding="utf-8")
    assert stages[0][0] == "ищу старые субтитры" and stages[-1][1] > 0.9


def test_strip_blurs_full_width_and_puts_ours_inside(captioned, tmp_path):
    heard = [Word(0.2, 0.6, "Губка"), Word(0.7, 1.2, "Боб")]
    out = tmp_path / "strip.mp4"
    rep = resub.replace_subtitles(captioned, out, lambda p: heard, tmp_path / "w", method="strip")
    info = probe(out)
    assert (info.width, info.height) == (W, H)                    # размер кадра не меняется
    assert rep["mode"] == "strip" and rep["method"] == "strip"
    lay = resub.analyze(captioned)
    a, b, size = resub.caption_strip(lay)
    assert a <= lay.bands[0][0] and b >= lay.bands[0][1] and b - a >= size * 1.9   # закрывает старый текст
    fc = resub.strip_filter(lay, [(a, b)])
    assert f"crop={W}:{b - a}:0:{a},gblur" in fc                  # от края до края
    ass = (tmp_path / "w" / "subs.ass").read_text(encoding="utf-8")
    margin = int(ass.split("Style: Cap,")[1].split(",")[20])
    assert a < H - margin < b + size                              # наш текст — внутри полосы


def test_letters_mask_takes_outlined_text_only():
    import numpy as np
    band = np.full((60, 400, 3), (40, 140, 60), np.uint8)              # зелёный фон
    band[20:40, 50:70] = 0                                            # буква: светлое пятно в чёрной обводке
    band[24:36, 54:66] = 255
    band[10:50, 200:300] = 255                                        # большое белое пятно без обводки
    m = resub.letters_mask(band, 400)
    assert m[30, 60] and m[21, 60] and m[30, 51] and not m[30, 250] and not m[5, 380]   # буква с обводкой — да, пятно — нет
    out = resub.erase(band, m)
    assert out[30, 60].tolist() != [255, 255, 255] and (out[:, 200:300] == band[:, 200:300]).all()


def test_erase_keeps_frame_and_removes_old_letters(captioned, tmp_path):
    import numpy as np
    heard = [Word(0.2, 0.6, "Губка"), Word(0.7, 1.2, "Боб")]
    out = tmp_path / "erase.mp4"
    rep = resub.replace_subtitles(captioned, out, lambda p: heard, tmp_path / "w")      # по умолчанию — стереть
    info = probe(out)
    assert (info.width, info.height) == (W, H) and rep["mode"] == "erase" and abs(info.duration - 12) < 0.5
    lay = resub.analyze(captioned)
    a, b = lay.bands[0]
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-ss", "5.25", "-i", str(out), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True).stdout
    frame = np.frombuffer(raw, np.uint8).reshape(H, W, 3)
    raw0 = subprocess.run([ffmpeg_exe(), "-v", "error", "-ss", "5.25", "-i", str(captioned), "-frames:v", "1",
                           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True).stdout
    before = resub.letters_mask(np.frombuffer(raw0, np.uint8).reshape(H, W, 3)[a:b], W).sum()
    after = resub.letters_mask(frame[a:b], W).sum()
    assert before > 0 and after < before * 0.2                     # старых букв почти не осталось


# ---------- в боте: задача «subs» в очереди обрезки ----------

@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OWNER_ID", "777")
    from reuploader.bot import cutjobs
    from reuploader.bot.db import DB
    from reuploader.bot.settings import load_settings
    monkeypatch.setattr(cutjobs, "whisper_transcribe", lambda *a, **k: [Word(0.2, 0.8, "привет.")])
    s = load_settings()
    db = DB(s.db_path)
    sent = []

    class TG:
        async def send(self, chat, text, buttons=None):
            sent.append((text, buttons)); return {"message_id": 1}

        async def send_video(self, chat, path, caption=None, *a):
            sent.append(("<video>", caption))

        async def call(self, *a, **k):
            return {}
    w = cutjobs.CutWorker(db, s, SimpleNamespace(tg=TG(), owner_id=777, public_url="https://x"))
    return db, s, w, sent


@pytest.mark.parametrize("mode,height,said", [("subs", H, "стёр"), ("subs_strip", H, "размытой полоской"),
                                              ("subs_crop", 1920, "обрезал полосу")])
def test_subs_job_runs_through_queue(worker, captioned, mode, height, said):
    from reuploader.bot.cutjobs import job_dir
    db, s, w, sent = worker
    jid = db.create_cut_job(777, "krabs.mp4", 1, status="queued")
    d = job_dir(s, jid)
    d.mkdir(parents=True)
    shutil.copy(captioned, d / "src.mp4")
    db.update_cut_job(jid, src_path=str(d / "src.mp4"), mode=mode)
    asyncio.run(w.run(db.cut_job(jid)))
    job = db.cut_job(jid)
    assert job["status"] == "done" and probe(job["out_path"]).height == height
    assert ("<video>", "🔤 krabs.mp4") in sent and "Субтитры заменены" in sent[-1][0]
    assert said in sent[-1][0] and not (d / "src.mp4").exists()   # исходник удалён


def test_subs_mode_via_api(worker, captioned):
    from aiohttp.test_utils import TestClient, TestServer
    from reuploader.bot.cutjobs import job_dir
    from reuploader.bot.web import WebApp
    from tests.test_e2e_helpers import init_data
    db, s, w, sent = worker
    jid = db.create_cut_job(777, "a.mp4", 1, status="uploaded")
    d = job_dir(s, jid)
    d.mkdir(parents=True)
    shutil.copy(captioned, d / "src.mp4")
    db.update_cut_job(jid, src_path=str(d / "src.mp4"))
    bot = SimpleNamespace(owner_id=777, has_access=lambda u: u == 777, app_key="k", cut=w,
                          stories=SimpleNamespace())

    async def go():
        async with TestClient(TestServer(WebApp(db, s, SimpleNamespace(poke=lambda: None), bot).build())) as c:
            h = {"X-Init-Data": init_data(777)}
            r1 = await (await c.post(f"/api/cut/{jid}/run", json={"mode": "subs"}, headers=h)).json()
            db.update_cut_job(jid, status="uploaded")
            r2 = await c.post(f"/api/cut/{jid}/run", json={"mode": "subs", "method": "crop"}, headers=h)
            return r1, r2.status, await r2.json()
    strip, status, crop = asyncio.run(go())
    assert strip["status"] == "queued" and strip["mode"] == "subs"           # по умолчанию — стереть
    assert status == 200 and crop["mode"] == "subs_crop"
