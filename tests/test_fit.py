import asyncio
import shutil
from types import SimpleNamespace

import pytest

from reuploader import pipeline, source
from reuploader.smartcut.media import probe
from tests.synth import make_video
from tests.test_smartcut import SCENES


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    p = tmp_path_factory.mktemp("fit") / "clip.mp4"
    _, _, words, _ = make_video(p, SCENES)
    return p, words


def fake_download(clip):
    def dl(url, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / "vid.src.mp4"
        shutil.copy(clip, dst)
        return dst, {"id": "vid", "title": "t", "description": "", "tags": [], "view_count": 1, "duration": 59}
    return dl


FX = {"zoom": 1.05, "rotate_deg": -0.5, "shadows": 0.1, "edge_blur": {"height": 0.1, "sigma": 18}, "crf": 28}


def test_reupload_fits_length(clip, tmp_path, monkeypatch):
    src, words = clip
    monkeypatch.setattr(source, "download", fake_download(src))
    _, out, meta = pipeline.prepare("u", tmp_path / "w", FX, fit_target=30, transcriber=lambda p: list(words))
    assert meta["fit"]["status"].startswith("ok")
    assert 28.5 - 0.2 <= probe(out).duration <= 31.5 + 0.2


def test_fit_error_uploads_full_video(clip, tmp_path, monkeypatch):
    src, _ = clip
    monkeypatch.setattr(source, "download", fake_download(src))

    def broken(path):
        raise RuntimeError("whisper сломался")
    _, out, meta = pipeline.prepare("u", tmp_path / "w", FX, fit_target=30, transcriber=broken)
    assert meta["fit"]["status"] == "error" and "whisper" in meta["fit"]["error"]
    assert probe(out).duration > 55


def test_no_fit_by_default(clip, tmp_path, monkeypatch):
    src, _ = clip
    monkeypatch.setattr(source, "download", fake_download(src))
    _, out, meta = pipeline.prepare("u", tmp_path / "w", FX)
    assert meta["fit"] is None and probe(out).duration > 55


def test_fit_target_modes(tmp_path, monkeypatch):
    from reuploader.bot import worker
    from reuploader.bot.db import DB
    db = DB(tmp_path / "b.db")
    pid = db.create_project("p", 1)
    db.add_source(pid, "https://www.youtube.com/@src")
    assert worker.fit_target_for(db, db.project(pid)) is None                     # по умолчанию выкл
    db.update_project(pid, fit_mode="fixed", fit_seconds=45)
    assert worker.fit_target_for(db, db.project(pid)) == (45, 0.05)
    calls = []

    def list_shorts(url, n):
        calls.append(url)
        if "/channel/" in url:        # у нового канала мало роликов -> берём источники
            return [{"duration": 10, "view_count": 5}]
        return [{"duration": d, "view_count": v} for d, v in
                [(58, 900), (40, 800), (35, 700), (70, 10), (20, 5), (15, 4), (30, 3), (25, 2), (22, 1), (12, 1)]]
    monkeypatch.setattr(source, "list_shorts", list_shorts)
    db.update_project(pid, fit_mode="channel", channel_id="UCxxx")
    assert worker.fit_target_for(db, db.project(pid)) == (40, 0.05)               # медиана 58, 40, 35
    assert worker.fit_target_for(db, db.project(pid))[0] == 40 and len(calls) == 2   # второй раз — из кэша


def test_auto_publish_button_creates_auto_pick_slot(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(tmp_path))
    from aiohttp.test_utils import TestClient, TestServer
    monkeypatch.setenv("BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("OWNER_ID", "777")
    from reuploader.bot.db import DB
    from reuploader.bot.settings import load_settings
    from reuploader.bot.web import WebApp
    from tests.test_e2e_helpers import init_data
    s = load_settings()
    db = DB(s.db_path)
    pid = db.create_project("p", 777)
    db.update_project(pid, delivery="telegram")
    pokes = []
    sched = SimpleNamespace(poke=lambda: pokes.append(1), plan_day=lambda *a, **k: None)
    bot = SimpleNamespace(owner_id=777, has_access=lambda u: u == 777, app_key="k")

    async def go():
        async with TestClient(TestServer(WebApp(db, s, sched, bot).build())) as c:
            h = {"X-Init-Data": init_data(777)}
            r1 = await c.post(f"/api/projects/{pid}/publish", json={"when": "auto"}, headers=h)
            db.add_source(pid, "https://www.youtube.com/@src")
            r2 = await c.post(f"/api/projects/{pid}/publish", json={"when": "auto"}, headers=h)
            return (r1.status, await r1.json()), (r2.status, await r2.json())
    (s1, j1), (s2, j2) = asyncio.run(go())
    assert s1 == 400 and "источники" in j1["error"]
    assert s2 == 200 and pokes
    slot = db.q("SELECT * FROM slots WHERE project_id = ?", pid)[0]
    assert slot["kind"] == "manual" and slot["video_url"] is None


def test_parse_range_and_fit_params():
    from reuploader.smartcut.target import fit_params, parse_duration, parse_range
    assert parse_duration("1.35") == 95 and parse_duration("1:35") == 95 and parse_duration("12.5") == 12.5
    assert parse_range("1.35-2.35") == (95, 155) == parse_range("от 1:35 до 2:35") == parse_range("2:35 – 1:35")
    assert parse_range("0:45") == (45, 45)
    t, tol = fit_params(95, 155)
    assert round(t * (1 - tol)) == 95 and round(t * (1 + tol)) == 155


def test_range_fit_lands_inside_and_leaves_short_alone(clip, tmp_path, monkeypatch):
    from reuploader.smartcut.target import fit_params
    src, words = clip
    monkeypatch.setattr(source, "download", fake_download(src))
    t, tol = fit_params(25, 40)
    _, out, meta = pipeline.prepare("u", tmp_path / "a", FX, t, lambda p: list(words), tol)
    assert meta["fit"]["status"].startswith("ok") and 25 - 0.2 <= probe(out).duration <= 40 + 0.2
    t, tol = fit_params(50, 70)                     # 59 с уже внутри диапазона — не режем
    _, out, meta = pipeline.prepare("u", tmp_path / "b", FX, t, lambda p: list(words), tol)
    assert meta["fit"]["status"] == "already_short" and probe(out).duration > 55


def test_worker_range_mode(tmp_path):
    from reuploader.bot import worker
    from reuploader.bot.db import DB
    db = DB(tmp_path / "b.db")
    pid = db.create_project("p", 1)
    db.update_project(pid, fit_mode="range", fit_min=95, fit_max=155)
    t, tol = worker.fit_target_for(db, db.project(pid))
    assert round(t * (1 - tol)) == 95 and round(t * (1 + tol)) == 155
