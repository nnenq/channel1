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


# ---------- фоновая музыка и оформление ----------

@pytest.fixture(scope="module")
def track(tmp_path_factory):
    p = tmp_path_factory.mktemp("music") / "calm beat.mp3"
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:sample_rate=44100",
                    "-t", "30", "-c:a", "libmp3lame", "-b:a", "96k", str(p)], check=True)
    return p


def test_audio_graph_variants():
    from reuploader import music
    both = music.audio_graph("0:a", 1, 60, "low")
    assert "sidechaincompress" in both and "volume=0.35," in both and "afade=t=out:st=58.00" in both
    assert both.endswith("[a]") and "loudnorm=I=-14" in both
    assert "sidechain" not in music.audio_graph(None, 1, 30)            # звука нет — только музыка
    assert music.audio_graph("0:a", None, 30).startswith("[0:a]")       # музыки нет — только выравнивание
    assert music.audio_graph(None, None, 30) is None
    assert music.start_offset(30, 60) == 0.0 and 0 <= music.start_offset(200, 60) <= 60


def test_music_mixed_into_result(captioned, track, tmp_path):
    out = tmp_path / "m.mp4"
    rep = resub.replace_subtitles(captioned, out, lambda p: [Word(0.2, 0.8, "привет")], tmp_path / "w",
                                  method="strip", music=track, music_level="high")
    info = probe(out)
    assert rep["music"] == "calm beat.mp3" and not rep["enhance"] and info.audio_streams == 1
    assert abs(info.duration - 12) < 0.5                                  # трек длиннее — ролик не удлиняется
    err = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(out), "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    lufs = float(err.split("Integrated loudness:")[1].split("I:")[1].split("LUFS")[0])
    assert -17 < lufs < -11                                              # громкость выровнена под YouTube


def test_music_api_upload_prefs_delete(worker, track):
    from aiohttp.test_utils import TestClient, TestServer
    from reuploader.bot.cutjobs import music_dir
    from reuploader.bot.web import WebApp
    from tests.test_e2e_helpers import init_data
    db, s, w, sent = worker
    bot = SimpleNamespace(owner_id=777, has_access=lambda u: u in (777, 555), app_key="k", cut=w,
                          stories=SimpleNamespace())

    async def go():
        async with TestClient(TestServer(WebApp(db, s, SimpleNamespace(poke=lambda: None), bot).build())) as c:
            h, other = {"X-Init-Data": init_data(777)}, {"X-Init-Data": init_data(555)}
            r = {"empty": await (await c.get("/api/music", headers=h)).json()}
            r["bad"] = (await c.put("/api/music?name=virus.exe", data=b"x" * 100, headers=h)).status
            r["fake"] = (await c.put("/api/music?name=fake.mp3", data=b"x" * 5000, headers=h)).status
            r["up"] = await (await c.put("/api/music?name=../../calm beat.mp3", data=track.read_bytes(),
                                         headers=h)).json()
            r["exists"] = (music_dir(s, 777) / "calm beat.mp3").exists() and not list(music_dir(s, 777).glob("*.part"))
            r["file"] = (await c.get("/api/music/calm%20beat.mp3", headers=h)).status
            r["foreign"] = (await c.get("/api/music/calm%20beat.mp3", headers=other)).status
            r["prefs"] = await (await c.patch("/api/music", json={"music_level": "low", "enhance": False,
                                                                  "music_track": "calm beat.mp3"}, headers=h)).json()
            r["badtrack"] = await (await c.patch("/api/music", json={"music_track": "nope.mp3"}, headers=h)).json()
            r["builtin"] = (await c.get("/api/music/builtin:fun", headers=h)).status
            r["delbuiltin"] = (await c.delete("/api/music/builtin:fun", headers=h)).status
            r["del"] = await (await c.delete("/api/music/calm%20beat.mp3", headers=h)).json()
            return r
    r = asyncio.run(go())
    assert r["empty"]["tracks"] == [] and r["empty"]["music_on"] == 1 and r["empty"]["music_level"] == "mid"
    assert r["bad"] == 400 and r["fake"] == 400
    assert [t["name"] for t in r["up"]["tracks"]] == ["calm beat.mp3"]                  # имя без «../»
    assert r["exists"] and not (music_dir(s, 777) / "calm beat.mp3").exists()          # удалён в конце
    assert r["file"] == 200 and r["foreign"] == 404                                    # чужие треки не видно
    assert r["prefs"]["music_level"] == "low" and r["prefs"]["enhance"] == 0
    assert r["prefs"]["music_track"] == "calm beat.mp3" and r["badtrack"]["music_track"] == "calm beat.mp3"
    assert r["builtin"] == 200 and r["delbuiltin"] == 400 and len(r["empty"]["builtin"]) == 2
    assert r["del"]["tracks"] == []


def test_subs_job_uses_users_music(worker, captioned, track):
    from reuploader.bot.cutjobs import job_dir, music_dir
    db, s, w, sent = worker

    def job():
        jid = db.create_cut_job(777, "krabs.mp4", 1, status="queued")
        d = job_dir(s, jid)
        d.mkdir(parents=True)
        shutil.copy(captioned, d / "src.mp4")
        db.update_cut_job(jid, src_path=str(d / "src.mp4"), mode="subs_strip")
        asyncio.run(w.run(db.cut_job(jid)))
        return db.cut_job(jid)
    assert job()["status"] == "done" and "🎵 Фоновая музыка: Встроенная: весёлая" in sent[-1][0]  # своих нет
    music_dir(s, 777).mkdir(parents=True)
    shutil.copy(track, music_dir(s, 777) / "calm beat.mp3")
    assert job()["status"] == "done" and "🎵 Фоновая музыка: calm beat.mp3" in sent[-1][0]
    db.set_prefs(777, music_track="builtin:mystery")
    assert job()["status"] == "done" and "Встроенная: загадочная" in sent[-1][0]          # выбрана вручную
    db.set_prefs(777, music_on=0)
    job()
    assert "🎵" not in sent[-1][0]                                                       # музыку выключили


def test_builtin_melody_is_clean_loop(tmp_path):
    import numpy as np
    from reuploader import music_builtin as mb
    for key, p in mb.PRESETS.items():
        x = mb.synth(key)
        beat = 60 / p["bpm"]
        assert abs(len(x) / mb.SR - beat * 8 * len(p["chords"]) * mb.ROUNDS) < 0.01   # целое число тактов
        assert np.abs(x).max() <= 0.9 and np.sqrt(np.mean(x ** 2)) > 0.05            # не клиппует, не тишина
    f = mb.ensure(tmp_path, "fun")
    assert f.exists() and probe(f).duration > 60 and mb.ensure(tmp_path, "fun") == f    # кэш


def test_resolve_track_choice(tmp_path, track):
    from reuploader import music
    own, built = tmp_path / "own", tmp_path / "built"
    assert music.resolve("", own, built)[1] == "Встроенная: весёлая"                    # своих нет
    own.mkdir()
    shutil.copy(track, own / "a.mp3")
    assert music.resolve("", own, built) == (own / "a.mp3", "a.mp3")
    assert music.resolve("a.mp3", own, built)[1] == "a.mp3"
    assert music.resolve("builtin:mystery", own, built)[1] == "Встроенная: загадочная"
    assert music.resolve("deleted.mp3", own, built)[1] == "a.mp3"                     # удалённый — случайный
    assert music.resolve("../../etc/passwd", own, built)[1] == "a.mp3"


def test_music_is_actually_audible_under_voice(captioned, tmp_path):
    """Регрессия: раньше музыка оказывалась на 35 дБ тише голоса — её не было слышно."""
    import re

    import numpy as np
    from reuploader import music_builtin as mb

    track = mb.ensure(tmp_path / "b", "fun")

    def notes_peak(path):
        raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000",
                              "-f", "s16le", "-"], capture_output=True).stdout
        a = np.frombuffer(raw, np.int16) / 32768
        spec = np.abs(np.fft.rfft(a))
        f = np.fft.rfftfreq(len(a), 1 / 16000)
        band = lambda lo, hi: spec[(f > lo) & (f < hi)].sum()          # noqa: E731
        notes = [130.8, 196.0, 220.0, 174.6, 261.6, 392.0, 523.3]       # аккорды встроенной мелодии
        return np.mean([band(n - 1.5, n + 1.5) / max(band(n - 12, n - 4) + band(n + 4, n + 12), 1e-9) * 4
                        for n in notes])
    plain, mixed = tmp_path / "plain.mp4", tmp_path / "mixed.mp4"
    resub.replace_subtitles(captioned, plain, lambda p: [], tmp_path / "w1", method="strip")
    resub.replace_subtitles(captioned, mixed, lambda p: [], tmp_path / "w2", method="strip", music=track,
                            music_level="low")
    assert notes_peak(mixed) > 2 * notes_peak(plain)                     # мелодию слышно даже на «тихо»
    # и она не громче голоса: громкость ролика держится на уровне голоса
    err = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(mixed), "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    assert -17 < float(re.findall(r"I:\s+(-?[\d.]+) LUFS", err)[-1]) < -11


def test_send_video_has_preview_size_and_duration(captioned, tmp_path):
    """Регрессия: без превью и размеров Telegram показывал чёрный квадрат вместо видео."""
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from reuploader.bot.telegram import TG
    got = {}

    async def send_video(request):
        reader = await request.multipart()
        async for part in reader:
            data = await part.read()
            got[part.name] = data if part.name in ("video", "thumbnail") else data.decode()
        return web.json_response({"ok": True, "result": {"message_id": 5}})

    async def go():
        app = web.Application(client_max_size=200 * 1024 * 1024)
        app.router.add_post("/botX/sendVideo", send_video)
        async with TestServer(app) as srv, aiohttp.ClientSession() as session:
            tg = TG("X", session)
            tg.base = str(srv.make_url("/botX/"))
            video = tmp_path / "v.mp4"
            shutil.copy(captioned, video)
            await tg.send_video(1, str(video), "✅ готово")
            return list(tmp_path.glob("*.thumb.jpg"))
    left = asyncio.run(go())
    assert got["width"] == str(W) and got["height"] == str(H) and got["duration"] == "12"
    assert got["thumbnail"][:2] == b"\xff\xd8" and len(got["thumbnail"]) < 200 * 1024     # JPEG-превью
    assert len(got["video"]) > 10000 and got["supports_streaming"] == "true" and not left   # превью удалено


def test_phone_video_with_rotation_flag(captioned, tmp_path):
    """Регрессия: видео с iPhone (.mov) хранится «лёжа» с пометкой поворота — размеры и полоса
    субтитров должны считаться по повёрнутому кадру, иначе картинка ломалась, а субтитры не находились."""
    land, mov = tmp_path / "land.mp4", tmp_path / "phone.mov"
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-i", str(captioned), "-vf", "transpose=1",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy", str(land)], check=True)
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-display_rotation", "90", "-i", str(land),
                    "-c", "copy", str(mov)], check=True)
    info = probe(mov)
    assert (info.width, info.height) == (W, H)                      # как его видит зритель
    assert resub.analyze(mov).bands == resub.analyze(captioned).bands
    out = tmp_path / "out.mp4"
    rep = resub.replace_subtitles(mov, out, lambda p: [Word(0.2, 0.8, "привет")], tmp_path / "w")
    assert rep["mode"] == "erase" and (probe(out).width, probe(out).height) == (W, H)


def test_no_ffmpeg_options_removed_in_new_versions():
    """Регрессия: в новом ffmpeg нет опции -vsync — из-за неё бот присылал видео без картинки."""
    from pathlib import Path
    root = Path(resub.__file__).parent
    bad = [str(p.relative_to(root)) for p in root.rglob("*.py") if '"-vsync"' in p.read_text(encoding="utf-8")]
    assert bad == []


def test_iphone_hdr_and_variable_fps(captioned, tmp_path):
    """HDR (HLG) с iPhone -> обычные цвета с пометкой BT.709; «плавающая» частота кадров не ломает видео."""
    hdr, vfr = tmp_path / "hdr.mov", tmp_path / "vfr.mp4"
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-i", str(captioned), "-vf", "format=yuv420p10le",
                    "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
                    "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc",
                    "-c:a", "aac", str(hdr)], check=True)
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-i", str(captioned), "-vf",
                    "select='lt(n\\,30)+not(mod(n\\,4))'", "-fps_mode", "vfr", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(vfr)], check=True)
    assert probe(hdr).hdr and not probe(captioned).hdr
    for src in (hdr, vfr):
        for method in ("erase", "strip"):
            out = tmp_path / f"{src.stem}_{method}.mp4"
            resub.replace_subtitles(src, out, lambda p: [Word(0.2, 0.8, "привет")], tmp_path / "w", method=method)
            head = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
            assert "bt709" in head and "arib" not in head                 # обычные цвета, без HDR-пометки
            assert abs(probe(out).duration - 12) < 0.5 and probe(out).height == H


def _hard_captions(tmp_path, outline="&H00000000", border=4):
    """Чистое видео + то же видео со вшитыми субтитрами (слово за словом, жёлтое выделение)."""
    clean, burned, ass = tmp_path / "clean.mp4", tmp_path / "burned.mp4", tmp_path / "old.ass"
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"life=s={W}x{H}:r=25:ratio=0.4:mold=8:life_color=#3aa0ff:death_color=#204020:mold_color=#c06030",
                    "-f", "lavfi", "-i", "sine=f=300:sample_rate=44100", "-t", "10",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-c:a", "aac", "-shortest", str(clean)],
                   check=True)
    words = "SQUIDWARD COVERED HIS ENTIRE BODY IN CEMENT JUST TO HIDE HIMSELF THEN HE ACCIDENTALLY FELL".split()
    lines = []
    for i in range(0, len(words), 2):
        pair = words[i:i + 2]
        for k in range(len(pair)):
            t0, t1 = (i + k) * 0.6, (i + k + 1) * 0.6
            txt = " ".join(("{\\c&H0000E5FF&}" + w + "{\\r}") if j == k else w for j, w in enumerate(pair))
            lines.append(f"Dialogue: 0,0:00:{t0:05.2f},0:00:{t1:05.2f},Cap,,0,0,0,,{txt}")
    ass.write_text(f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial,34,&H00FFFFFF,&H00FFFFFF,{outline},&H80000000,-1,0,0,0,100,100,1,0,1,{border},2,2,20,20,200,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(lines) + "\n", encoding="utf-8")
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-i", str(clean), "-vf", f"ass={ass.name}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-c:a", "copy", str(burned)],
                   check=True, cwd=tmp_path)
    return clean, burned


def _gray_frames(path):
    import numpy as np
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(path), "-vf", "fps=25", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(int)


@pytest.mark.parametrize("outline,border", [("&H00000000", 4), ("&H00505050", 2)])   # чёрная и серая тонкая обводка
def test_erase_leaves_no_flashes_of_old_captions(tmp_path, outline, border):
    """Регрессия: в отдельных кадрах проскакивали буквы старых субтитров («вспышки»)."""
    import numpy as np
    clean, burned = _hard_captions(tmp_path, outline, border)
    out = tmp_path / "out.mp4"
    rep = resub.replace_subtitles(burned, out, lambda p: [], tmp_path / "w", method="erase")
    assert rep["mode"] == "erase"
    C, B, O = _gray_frames(clean), _gray_frames(burned), _gray_frames(out)
    n = min(len(C), len(B), len(O))
    text = np.abs(B[:n] - C[:n]) > 60                       # где в каждом кадре были старые буквы
    left = []
    for i in range(n):
        if text[i].sum() < 50:
            continue
        # «осталась буква» — пиксель на месте старого текста всё ещё близок к нему, а не к чистой картинке
        still = text[i] & (np.abs(O[i] - B[i]) < 25) & (np.abs(O[i] - C[i]) > 60)
        left.append(still.sum() / text[i].sum())
    assert left and max(left) < 0.03, f"в худшем кадре осталось {max(left):.1%} старых букв"


def test_two_line_captions_are_erased(tmp_path):
    """Регрессия: фраза в две строки — верхняя строка выше обычной полосы и раньше оставалась."""
    import numpy as np
    clean, burned = _hard_captions(tmp_path)
    ass = (tmp_path / "old.ass").read_text(encoding="utf-8")
    # редкие события — в две строки (\N), как у длинных фраз
    lines = ass.splitlines()
    ev = [i for i, ln in enumerate(lines) if ln.startswith("Dialogue:")]
    for k in ev[2::7]:
        lines[k] = lines[k].replace("{\\r} ", "{\\r}\\N", 1) if "{\\r} " in lines[k] else lines[k].replace(" ", "\\N", 1)
    (tmp_path / "old.ass").write_text("\n".join(lines) + "\n", encoding="utf-8")
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-i", str(clean), "-vf", "ass=old.ass", "-c:v", "libx264",
                    "-preset", "ultrafast", "-crf", "16", "-c:a", "copy", str(burned)], check=True, cwd=tmp_path)
    out = tmp_path / "out.mp4"
    resub.replace_subtitles(burned, out, lambda p: [], tmp_path / "w", method="erase")
    C, B, O = _gray_frames(clean), _gray_frames(burned), _gray_frames(out)
    n = min(len(C), len(B), len(O))
    worst = 0
    for i in range(n):
        text = np.abs(B[i] - C[i]) > 60
        if text.sum() >= 50:
            still = text & (np.abs(O[i] - B[i]) < 25) & (np.abs(O[i] - C[i]) > 60)
            worst = max(worst, still.sum() / text.sum())
    assert worst < 0.03, f"в худшем кадре осталось {worst:.1%} старых букв"


# ---------- «всё сразу»: длина -> уникализация -> кадр + субтитры + музыка ----------

def test_combo_options_validation():
    from reuploader import combo
    assert combo.clean({}) == combo.DEFAULT
    o = combo.clean({"frame": "erase", "subs": 0, "uniq": 1, "trim": " 0:58 ", "junk": 1})
    assert o == {"frame": "erase", "subs": False, "uniq": True, "trim": "0:58", "loop": True}
    for bad in ({"frame": "hack"}, {"trim": "abc"}):
        with pytest.raises(ValueError):
            combo.clean(bad)
    assert combo.describe(combo.DEFAULT, music=True) == "Как в CapCut · наши субтитры · уникализация · петля · музыка"


@pytest.fixture(scope="module")
def speech_clip(tmp_path_factory):
    """Видео с «речью» и паузами — чтобы было что сокращать."""
    from tests.synth import make_video
    from tests.test_smartcut import SCENES
    p = tmp_path_factory.mktemp("sp") / "speech.mp4"
    make_video(p, SCENES)
    return p


def test_combo_runs_all_steps(speech_clip, track, tmp_path):
    from reuploader import combo
    src_dur = probe(speech_clip).duration
    heard = [Word(i * 0.5, i * 0.5 + 0.4, "слово" + ("." if i % 4 == 3 else "")) for i in range(20)]
    stages = []
    out = tmp_path / "out.mp4"
    target = f"0:{int(src_dur * 0.7):02d}"
    rep = combo.run(speech_clip, out, combo.clean({"frame": "capcut", "subs": True, "uniq": True, "trim": target}),
                    lambda cache: ((lambda p: []) if "src" in str(cache) else (lambda p: heard)), tmp_path / "w",
                    progress=lambda st, f: stages.append(f), music=track, music_level="mid")
    info = probe(out)
    assert (info.width, info.height) == (1080, 1920) and info.audio_streams == 1     # кадр как в CapCut
    assert rep["frame"] == "capcut" and rep["words"] > 0 and rep["music"] == "calm beat.mp3"
    assert rep["uniq"]["zoom"] and rep["trim"]["after"] < rep["trim"]["before"] and info.duration < src_dur * 0.9
    assert stages == sorted(stages) and stages[-1] > 0.9                              # проценты только растут


def test_combo_keeps_going_when_cannot_shorten(captioned, tmp_path):
    from reuploader import combo
    rep = combo.run(captioned, tmp_path / "o.mp4", combo.clean({"frame": "keep", "subs": False, "uniq": False,
                                                                 "trim": "0:05"}),
                    lambda cache: (lambda p: []), tmp_path / "w")
    assert rep["trim"]["status"] == "failed" and "пауз" in rep["trim"]["why"]
    assert abs(probe(tmp_path / "o.mp4").duration - 12) < 0.5


def test_combo_job_through_queue_and_saved_choice(worker, captioned):
    from aiohttp.test_utils import TestClient, TestServer
    from reuploader.bot.cutjobs import job_dir
    from reuploader.bot.web import WebApp
    from tests.test_e2e_helpers import init_data
    db, s, w, sent = worker
    jid = db.create_cut_job(777, "kenny.mp4", 1, status="uploaded")
    d = job_dir(s, jid)
    d.mkdir(parents=True)
    shutil.copy(captioned, d / "src.mp4")
    db.update_cut_job(jid, src_path=str(d / "src.mp4"))
    bot = SimpleNamespace(owner_id=777, has_access=lambda u: u == 777, app_key="k", cut=w, stories=SimpleNamespace())

    async def go():
        async with TestClient(TestServer(WebApp(db, s, SimpleNamespace(poke=lambda: None), bot).build())) as c:
            h = {"X-Init-Data": init_data(777)}
            bad = (await c.post(f"/api/cut/{jid}/run", json={"mode": "combo", "options": {"trim": "abc"}},
                                headers=h)).status
            r = await (await c.post(f"/api/cut/{jid}/run", json={"mode": "combo", "options": {
                "frame": "keep", "subs": True, "uniq": False}}, headers=h)).json()
            lst = await (await c.get("/api/cut", headers=h)).json()
            return bad, r, lst
    bad, r, lst = asyncio.run(go())
    assert bad == 400 and r["mode"] == "combo" and r["status"] == "queued"
    assert lst["combo"] == {"frame": "keep", "subs": True, "uniq": False, "trim": "", "loop": True}   # выбор запомнен
    db.set_prefs(777, music_on=0)
    asyncio.run(w.run(db.cut_job(jid)))
    job = db.cut_job(jid)
    assert job["status"] == "done" and probe(job["out_path"]).height == H              # «кадр как есть»
    assert "🖼 Кадр: Кадр как есть" in sent[-1][0] and "🔤 Наши субтитры" in sent[-1][0] and "🔁 Петля" in sent[-1][0]


def test_cancel_button_stops_job_and_keeps_video(worker, captioned):
    """«⏹ Отменить»: в очереди — снимается сразу; в работе — останавливается, видео остаётся для нового выбора."""
    import time

    from reuploader.bot.cutjobs import job_dir
    db, s, w, sent = worker
    jid = db.create_cut_job(777, "oops.mp4", 1, status="uploaded")
    d = job_dir(s, jid)
    d.mkdir(parents=True)
    shutil.copy(captioned, d / "src.mp4")
    db.update_cut_job(jid, src_path=str(d / "src.mp4"), mode="combo",
                      options='{"frame": "erase", "subs": true, "uniq": true, "loop": true}', status="queued")
    assert w.cancel(db.cut_job(jid))[0] and db.cut_job(jid)["status"] == "uploaded"       # из очереди
    assert not w.cancel(db.cut_job(jid))[0]                                                # уже нечего

    db.update_cut_job(jid, status="queued")
    offered = []

    async def offer(uid, j, name):
        offered.append((uid, j, name))
    w.bot.offer_video = offer

    async def go():
        task = asyncio.create_task(w.run(db.cut_job(jid)))
        while db.cut_job(jid)["status"] != "running" or (db.cut_job(jid)["progress"] or 0) < 0.05:
            await asyncio.sleep(0.05)
        t0 = time.time()
        assert w.cancel(db.cut_job(jid)) == (True, "Останавливаю…")
        await task
        return time.time() - t0
    took = asyncio.run(go())
    job = db.cut_job(jid)
    assert job["status"] == "uploaded" and job["stage"] == "отменено" and took < 15
    assert (d / "src.mp4").exists() and not (d / "out.mp4").exists()                      # видео осталось
    assert sent[-1][1] == [[{"text": "⏹ Отменить", "callback_data": f"cancel:{jid}"}]]     # кнопка в прогрессе
    assert offered == [(777, jid, "oops.mp4")] and jid not in w.stop
    # после отмены можно запустить снова — и всё доделывается
    db.update_cut_job(jid, status="queued")
    asyncio.run(w.run(db.cut_job(jid)))
    assert db.cut_job(jid)["status"] == "done"


def test_share_link_for_claude(worker, captioned):
    """«🔗 Ссылка для Claude»: открытая ссылка на видео любого размера; без входа, со сроком, уходит и в чат."""
    from aiohttp.test_utils import TestClient, TestServer
    from reuploader.bot.cutjobs import job_dir
    from reuploader.bot.web import WebApp
    from tests.test_e2e_helpers import init_data
    db, s, w, sent = worker
    jid = db.create_cut_job(777, "Губка Боб.mp4", 1, status="uploaded")
    d = job_dir(s, jid)
    d.mkdir(parents=True)
    shutil.copy(captioned, d / "src.mp4")
    db.update_cut_job(jid, src_path=str(d / "src.mp4"))
    bot = SimpleNamespace(owner_id=777, has_access=lambda u: u == 777, app_key="k", cut=w, stories=SimpleNamespace(),
                          public_url="https://bot.example", tg=w.bot.tg)

    async def go():
        async with TestClient(TestServer(WebApp(db, s, SimpleNamespace(poke=lambda: None), bot).build())) as c:
            h = {"X-Init-Data": init_data(777)}
            stranger = (await c.post(f"/api/cut/{jid}/share", json={})).status
            r = await (await c.post(f"/api/cut/{jid}/share", json={}, headers=h)).json()
            path = r["url"].replace("https://bot.example", "")
            full = await c.get(path)                                       # без входа в Telegram
            body = await full.read()
            part = await c.get(path, headers={"Range": "bytes=0-99"})
            bad = (await c.get(path.replace(path.split("/")[2], "nope"))).status
            db.update_cut_job(jid, share_until="2000-01-01T00:00:00+00:00")
            expired = (await c.get(path)).status
            return stranger, r, full.status, full.headers["Content-Type"], body, part.status, bad, expired
    stranger, r, st, ctype, body, part, bad, expired = asyncio.run(go())
    assert stranger == 401 and r["what"] == "src" and r["hours"] == s.cut_link_ttl_h
    assert st == 200 and ctype == "video/mp4" and body == (d / "src.mp4").read_bytes()
    assert part == 206 and bad == 404 and expired == 404
    assert r["url"] in sent[-1][0] and "Ссылка для Claude" in sent[-1][0]          # и в чат
