"""Удаление опубликованных роликов, пакетное сохранение настроек и перепланирование без «заливки сразу»."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from reuploader import uploader
from reuploader.bot.db import DB, from_iso, utcnow
from tests.test_e2e_helpers import init_data


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("OWNER_ID", "777")
    from reuploader.bot.settings import load_settings
    s = load_settings()
    db = DB(s.db_path)
    pid = db.create_project("p", 777)
    token = tmp_path / "tok.json"
    token.write_text("{}")
    db.update_project(pid, token_path=str(token))
    return s, db, pid


def call(s, db, sched, method, path, **kw):
    from reuploader.bot.web import WebApp
    bot = SimpleNamespace(owner_id=777, has_access=lambda u: u == 777, app_key="k")

    async def go():
        async with TestClient(TestServer(WebApp(db, s, sched, bot).build())) as c:
            r = await c.request(method, path, headers={"X-Init-Data": init_data(777)}, **kw)
            return r.status, await r.json()
    return asyncio.run(go())


def test_delete_upload_from_youtube_and_history(env, monkeypatch):
    s, db, pid = env
    db.add_upload(pid, "src", "aaaaaaaaaa1", "one", 10, "NEWNEWNEW01")
    db.add_upload(pid, "src", "aaaaaaaaaa2", "two", 10, "NEWNEWNEW02")
    deleted = []
    monkeypatch.setattr(uploader, "youtube_client", lambda t: "yt")
    monkeypatch.setattr(uploader, "delete_video", lambda yt, vid: deleted.append(vid) or True)
    sched = SimpleNamespace(poke=lambda: None, plan_day=lambda *a, **k: None)
    uid = db.uploads(pid)[0]["id"]
    st, j = call(s, db, sched, "DELETE", f"/api/projects/{pid}/uploads/{uid}")
    assert st == 200 and deleted == ["NEWNEWNEW02"] and "YouTube" in j["note"]
    assert [u["title"] for u in j["uploads"]] == ["one"]
    # удалённый исходник бот больше не возьмёт
    assert "aaaaaaaaaa2" in db.uploaded_ids(pid)
    # только из истории — YouTube не трогаем
    uid = db.uploads(pid)[0]["id"]
    st, j = call(s, db, sched, "DELETE", f"/api/projects/{pid}/uploads/{uid}?youtube=0")
    assert st == 200 and deleted == ["NEWNEWNEW02"] and j["uploads"] == []
    st, _ = call(s, db, sched, "DELETE", f"/api/projects/{pid}/uploads/{uid}")
    assert st == 404


def test_delete_without_rights_explains(env, monkeypatch):
    s, db, pid = env
    db.add_upload(pid, "src", "aaaaaaaaaa1", "one", 10, "NEWNEWNEW01")

    def no_rights(yt, vid):
        raise uploader.NoDeleteRights()
    monkeypatch.setattr(uploader, "youtube_client", lambda t: "yt")
    monkeypatch.setattr(uploader, "delete_video", no_rights)
    sched = SimpleNamespace(poke=lambda: None, plan_day=lambda *a, **k: None)
    st, j = call(s, db, sched, "DELETE", f"/api/projects/{pid}/uploads/{db.uploads(pid)[0]['id']}")
    assert st == 400 and "привяжи" in j["error"]
    assert len(db.uploads(pid)) == 1          # из истории не убрали


def test_other_users_upload_is_not_deletable(env):
    s, db, pid = env
    other = db.create_project("чужой", 555)
    db.add_upload(other, "src", "aaaaaaaaaa1", "x", 1, "")
    sched = SimpleNamespace(poke=lambda: None, plan_day=lambda *a, **k: None)
    st, _ = call(s, db, sched, "DELETE", f"/api/projects/{other}/uploads/{db.uploads(other)[0]['id']}")
    assert st == 404 and len(db.uploads(other)) == 1


def test_done_slot_links_to_upload(env):
    s, db, pid = env
    db.add_upload(pid, "src", "aaaaaaaaaa1", "one", 10, "NEWNEWNEW01")
    sid = db.add_slot(pid, "2000-01-01", utcnow().isoformat())
    db.set_slot(sid, "done", "https://youtube.com/shorts/NEWNEWNEW01")
    sched = SimpleNamespace(poke=lambda: None, plan_day=lambda *a, **k: None)
    _, j = call(s, db, sched, "GET", f"/api/projects/{pid}")
    done = [x for x in j["slots"] if x["status"] == "done"]
    assert done and done[0]["upload_id"] == db.uploads(pid)[0]["id"]


def test_batch_patch_applies_everything_at_once(env):
    s, db, pid = env
    calls = []
    sched = SimpleNamespace(poke=lambda: None, plan_day=lambda *a, **k: calls.append(k))
    st, j = call(s, db, sched, "PATCH", f"/api/projects/{pid}", json={
        "min_duration": 60, "max_duration": 100, "fit_mode": "range", "fit_range": "1:00-1:40",
        "per_day": 3, "effects": {"zoom": 1.1}})
    p = j["project"]
    assert st == 200 and (p["min_duration"], p["max_duration"], p["per_day"]) == (60, 100, 3)
    assert (p["fit_min"], p["fit_max"]) == (60, 100) and p["effects"]["zoom"] == 1.1
    assert len(calls) == 1                     # перепланировали один раз, а не на каждое поле
    # только фильтр длины — расписание не трогаем вообще
    calls.clear()
    call(s, db, sched, "PATCH", f"/api/projects/{pid}", json={"min_duration": 61, "max_duration": 100})
    assert calls == []


def test_replan_does_not_upload_right_away(env):
    s, db, pid = env
    from reuploader.bot.scheduler import REPLAN_MIN_DELAY, Scheduler
    db.update_project(pid, window_start="00:00", window_end="23:59", min_gap=1, max_gap=2, per_day=3)
    sched = Scheduler(db, s, None)
    now = sched.now_local()
    if now.hour == 23 and now.minute > 20:
        pytest.skip("окно дня почти закончилось")
    sched.plan_day(db.project(pid), force=True)
    slots = [x for x in db.q("SELECT * FROM slots WHERE project_id = ? AND status = 'planned'", pid)]
    assert slots
    first = min(from_iso(x["run_at"]) for x in slots)
    assert first - utcnow() >= timedelta(minutes=REPLAN_MIN_DELAY) - timedelta(seconds=5)


def _pick_env(tmp_path, monkeypatch, videos, **proj):
    from reuploader import pipeline, source
    from reuploader.bot import worker
    monkeypatch.setattr(source, "list_shorts", lambda url, limit=200: [dict(v) for v in videos])
    monkeypatch.setattr(source, "enrich", lambda vs, *a, **k: vs)

    def stop(url, *a, **k):
        raise RuntimeError("stop:" + url)
    monkeypatch.setattr(worker, "prepare", stop)
    db = DB(tmp_path / "p.db")
    pid = db.create_project("p", 1)
    db.add_source(pid, "https://www.youtube.com/@src")
    db.update_project(pid, delivery="telegram", **proj)
    sid = db.add_slot(pid, "2000-01-01", utcnow().isoformat())
    s = SimpleNamespace(work_dir=tmp_path / "w", whisper_model="base", data_dir=tmp_path)
    return worker.run_slot(db, s, db.slot(sid))


def _v(vid, views, age):
    return {"id": vid, "title": vid, "view_count": views, "age_days": age, "duration": 30,
            "published": None, "url": f"https://www.youtube.com/shorts/{vid}"}


VIDS = [_v("fresh50kkkk", 50_000, 2), _v("old2mmmmmmm", 2_000_000, 100), _v("old100kkkkk", 100_000, 50)]


def test_fresh_above_threshold_wins(tmp_path, monkeypatch):
    r = _pick_env(tmp_path, monkeypatch, VIDS + [_v("fresh300kkk", 300_000, 5)],
                  max_age_days=10, min_views=180_000, sort_by="views")
    assert "fresh300kkk" in r.info


def test_no_fresh_hit_takes_old_popular(tmp_path, monkeypatch):
    r = _pick_env(tmp_path, monkeypatch, VIDS, max_age_days=10, min_views=180_000, sort_by="trend")
    assert "old2mmmmmmm" in r.info


def test_fallback_off_or_nothing_above_threshold_is_exhausted(tmp_path, monkeypatch):
    r = _pick_env(tmp_path, monkeypatch, VIDS, max_age_days=10, min_views=180_000, fallback_old=False)
    assert r.status == "skipped" and r.exhausted and "180K" in r.info
    (tmp_path / "b").mkdir()
    r = _pick_env(tmp_path / "b", monkeypatch, VIDS, max_age_days=10, min_views=5_000_000)
    assert r.status == "skipped" and r.exhausted


def test_min_views_accepts_180k(env):
    s, db, pid = env
    sched = SimpleNamespace(poke=lambda: None, plan_day=lambda *a, **k: None)
    st, j = call(s, db, sched, "PATCH", f"/api/projects/{pid}", json={"min_views": "180K", "fallback_old": False})
    assert st == 200 and j["project"]["min_views"] == 180_000 and j["project"]["fallback_old"] is False
    st, _ = call(s, db, sched, "PATCH", f"/api/projects/{pid}", json={"min_views": "много"})
    assert st == 400


def test_same_video_not_sent_to_all_my_channels(tmp_path):
    db = DB(tmp_path / "x.db")
    a, b = db.create_project("a", 1), db.create_project("b", 1)
    other = db.create_project("чужой", 2)
    db.add_upload(a, "s", "vidAAAAAAAA", "t", 1, "N1")
    db.add_upload(other, "s", "vidOTHERRRR", "t", 1, "N2")
    assert db.exclude_ids(db.project(b)) == {"vidAAAAAAAA"}      # чужие проекты не влияют
    db.update_project(b, no_cross_dupes=False)
    assert db.exclude_ids(db.project(b)) == set()


def test_zero_view_autodelete(tmp_path):
    from reuploader.bot import zero_views
    from reuploader.bot.db import iso
    db = DB(tmp_path / "z.db")
    pid = db.create_project("p", 1)
    tok = tmp_path / "t.json"
    tok.write_text("{}")
    db.update_project(pid, token_path=str(tok), autodelete_zero=True, autodelete_hours=24)
    old = iso(utcnow() - timedelta(hours=30))
    for vid in ("ZERO0000000", "VIEWS000000", "PRIV0000000", "GONE0000000"):
        db.add_upload(pid, "s", "src" + vid, vid, 1, vid, live_at=old)
    db.add_upload(pid, "s", "srcFRESH", "fresh", 1, "FRESH000000")                       # < 24 ч
    db.add_upload(pid, "s", "srcANCIENT", "ancient", 1, "ANCIENT0000",
                  live_at=iso(utcnow() - timedelta(days=10)))                             # старый — не трогаем
    data = {"ZERO0000000": (0, "public"), "VIEWS000000": (5, "public"), "PRIV0000000": (0, "private"),
            "FRESH000000": (0, "public"), "ANCIENT0000": (0, "public")}

    class YT:
        def videos(self):
            return self

        def list(self, part, id):
            ids = id.split(",")
            return SimpleNamespace(execute=lambda: {"items": [
                {"id": i, "statistics": {"viewCount": str(data[i][0])}, "status": {"privacyStatus": data[i][1]}}
                for i in ids if i in data]})
    deleted = []
    rep = zero_views.run(db, lambda t: YT(), lambda yt, v: deleted.append(v), uploader.NoDeleteRights)
    assert deleted == ["ZERO0000000"]
    assert rep[0][1] == ["ZERO0000000"]
    left = {u["new_video_id"] for u in db.uploads(pid)}
    assert left == {"VIEWS000000", "PRIV0000000", "FRESH000000", "ANCIENT0000"}          # GONE — помечен удалённым
    assert zero_views.run(db, lambda t: YT(), lambda yt, v: deleted.append(v), uploader.NoDeleteRights) == []
    assert deleted == ["ZERO0000000"]                                                    # повторно не проверяет
