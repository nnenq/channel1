import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from aiohttp.test_utils import TestClient, TestServer

from reuploader import covers, uploader, effects
from reuploader.bot import worker
from reuploader.bot.db import DB, iso, utcnow
from reuploader.bot.settings import load_settings
from reuploader.bot.web import WebApp
from tests.test_e2e_helpers import init_data
from tests.synth import make_video


def image_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (600, 900), "#429AD4").save(buf, "PNG")
    return buf.getvalue()


def test_local_frames_and_templates(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network call"))
    video = tmp_path / "clip.mp4"
    make_video(video, [(color, [(1, 0, "hello")]) for color in ("red", "green", "blue")])
    frames = covers.extract_frames(video, tmp_path / "frames")
    assert len(frames) == 3
    colors = []
    for frame in frames:
        with Image.open(frame) as im:
            colors.append(max(range(3), key=lambda c: im.getpixel((100, 100))[c]))
    assert colors == [0, 1, 2]
    for style in covers.STYLES:
        output = covers.render(frames[0], "Куда исчез Крабс? Секрет бургера", style, tmp_path / f"{style}.jpg")
        with Image.open(output) as im:
            assert im.size == (1080, 1920)
        assert output.stat().st_size < 2 * 1024 * 1024
    assert "#" not in covers.suggested_text("Секрет бургера #shorts #cartoon")
    with pytest.raises(Exception):
        covers.normalize_image(b"not an image", tmp_path / "invalid.jpg")


def test_cover_api_ownership_binding_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    s = load_settings()
    db = DB(s.db_path)
    p = db.create_project("mine", 777)
    other = db.create_project("other", 888)
    db.update_project(p, delivery="telegram")
    sched = SimpleNamespace(poke=lambda: None, plan_day=lambda *a, **k: None)
    bot = SimpleNamespace(owner_id=777, has_access=lambda u: u in (777, 888), app_key="k")
    w = WebApp(db, s, sched, bot)
    url = "https://www.youtube.com/shorts/abcdefghijk"

    async def go():
        async with TestClient(TestServer(w.build())) as c:
            headers = {"X-Init-Data": init_data(777)}
            base = f"/api/projects/{p}/covers"
            r = await c.post(base, json={"video_url": url, "custom_only": True}, headers=headers)
            assert r.status == 202
            cid = (await r.json())["id"]
            assert (await c.get(f"{base}/{cid}")).status == 401
            assert (await c.get(f"{base}/{cid}", headers={"X-Init-Data": init_data(888)})).status == 404
            assert (await c.get(f"/api/projects/{other}/covers/{cid}", headers=headers)).status == 404
            assert (await c.post(f"{base}/{cid}/image", data=b"bad", headers=headers)).status == 400
            r = await c.post(f"{base}/{cid}/image", data=image_bytes(), headers=headers)
            assert r.status == 200 and (await r.json())["image"].startswith("data:image/jpeg")
            payload = {"video_url": url, "when": "now", "cover_choice": "selected", "cover_id": cid,
                       "cover_index": "custom", "style": "lemon", "text": "", "publication_title": "My title"}
            mismatch = payload | {"video_url": "https://youtu.be/zyxwvutsrqp"}
            assert (await c.post(f"/api/projects/{p}/publish", json=mismatch, headers=headers)).status == 400
            r = await c.post(f"/api/projects/{p}/publish", json=payload, headers=headers)
            assert r.status == 200
            slot = db.q("SELECT * FROM slots")[0]
            assert slot["publication_title"] == "My title"
            assert slot["cover_choice"] == "selected"
            assert Path(slot["cover_path"]).is_file()
            # Changing the draft after scheduling must not change the selected publication.
            before = Path(slot["cover_path"]).read_bytes()
            (w.covers.directory(cid) / "custom.jpg").write_bytes(b"changed")
            assert Path(slot["cover_path"]).read_bytes() == before
    asyncio.run(go())


def test_thumbnail_failure_and_telegram_failure_never_reupload(tmp_path, monkeypatch):
    db = DB(tmp_path / "b.db")
    p = db.create_project("p", 1)
    db.update_project(p, delivery="both", token_path="fake-token")
    image = tmp_path / "cover.jpg"
    covers.normalize_image(image_bytes(), image)
    sid = db.add_slot(p, "2026-09-28", iso(utcnow()), "manual", "https://youtu.be/abcdefghijk",
                      cover_path=str(image), cover_choice="selected", publication_title="Edited title")
    def prepare(url, work, *args):
        work.mkdir(parents=True, exist_ok=True)
        out = work / "video.mp4"
        out.write_bytes(b"fake-video")
        return out, out, {"id": "abcdefghijk", "title": "original", "description": "", "tags": [],
                          "duration": 10, "view_count": 5, "fit": None}
    monkeypatch.setattr(worker, "prepare", prepare)
    monkeypatch.setattr(uploader, "youtube_client", lambda *a: object())
    calls = []
    monkeypatch.setattr(uploader, "upload", lambda *a, **k: calls.append(a) or "uploaded-id")
    def fail(*a, **k):
        raise RuntimeError("rejected")
    monkeypatch.setattr(uploader, "set_thumbnail", fail)
    monkeypatch.setattr(effects, "shrink_to", fail)
    result = worker.run_slot(db, SimpleNamespace(work_dir=tmp_path / "work", data_dir=tmp_path), db.slot(sid))
    assert result.status == "done" and result.new_id == "uploaded-id"
    assert result.extra["cover_status"] == "manual"
    assert "Telegram" in result.extra["warning"]
    assert result.title == "Edited title" and len(calls) == 1
    assert db.uploaded_ids(p) == {"abcdefghijk"}
    assert Path(result.extra["cover"]).is_file()


def test_migration_preserves_cover_and_title(tmp_path):
    db = DB(tmp_path / "db")
    p = db.create_project("p", 1)
    sid = db.add_slot(p, "2026-09-28", iso(utcnow()), "manual", "https://youtu.be/abcdefghijk",
                      cover_path="selected.jpg", cover_choice="selected", publication_title="Title")
    db2 = DB(tmp_path / "db")
    assert db2.slot(sid)["cover_path"] == "selected.jpg"
    assert db2.project(p)["cover_mode"] == "off"
