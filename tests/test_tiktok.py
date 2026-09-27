import pytest
import yt_dlp

from reuploader import source

SEC = "MS4wLjABAAAAtestSecUid_123"
ENTRY = {"id": "7400000000000000001", "title": "кот", "view_count": 100, "duration": 20, "timestamp": 1758800000}


class FakeYDL:
    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def extract_info(self, url, download=False):
        if url.startswith("https://www.tiktok.com/@animal") and "/video/" not in url:
            raise yt_dlp.utils.DownloadError("ERROR: [tiktok:user] animal: Unable to extract secondary user ID.")
        if url == f"tiktokuser:{SEC}":
            return {"entries": [dict(ENTRY)]}
        if "/video/" in url:
            return {"channel_id": SEC, "uploader": "animal"}
        raise AssertionError(url)


@pytest.fixture(autouse=True)
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(source, "TIKTOK_IDS_FILE", tmp_path / "ids.json")


def test_profile_hidden_and_page_without_id_gives_clear_hint(monkeypatch):
    monkeypatch.setattr(source, "_tiktok_id_from_profile_page", lambda h: None)
    with pytest.raises(source.TikTokIdError, match="ссылку на ЛЮБОЕ видео"):
        source.list_shorts("https://www.tiktok.com/@animal")


def test_id_from_profile_page_is_used_and_cached(monkeypatch):
    monkeypatch.setattr(source, "_tiktok_id_from_profile_page", lambda h: SEC)
    vs = source.list_shorts("https://www.tiktok.com/@animal")
    assert vs[0]["id"] == ENTRY["id"] and source._tiktok_ids()["animal"] == SEC
    monkeypatch.setattr(source, "_tiktok_id_from_profile_page", lambda h: pytest.fail("должен взять из кэша"))
    assert source.list_shorts("https://www.tiktok.com/@animal")[0]["view_count"] == 100


def test_id_from_any_video_link(monkeypatch):
    monkeypatch.setattr(source, "_tiktok_id_from_profile_page", lambda h: None)
    handle, sec = source.tiktok_id_from_video("https://www.tiktok.com/@animal/video/7400000000000000001")
    assert (handle, sec) == ("animal", SEC)
    assert source.list_shorts("https://www.tiktok.com/@animal")[0]["title"] == "кот"


def test_snapshot_skips_broken_channel():
    from types import SimpleNamespace
    from reuploader.bot import trends
    rows = [{"url": "bad", "token_path": None}, {"url": "good", "token_path": None}]
    recorded = []
    db = SimpleNamespace(all_source_channels=lambda: rows, last_snapshot_at=lambda u: None,
                         add_snapshots=lambda ch, v, at: recorded.append(ch), prune_snapshots=lambda t: None)

    def list_shorts(url, n):
        if url == "bad":
            raise RuntimeError("TikTok сломался")
        return [{"id": "x", "view_count": 1}]
    trends.snapshot_all(db, list_shorts, None, None)
    assert recorded == ["good"]


def test_broken_source_is_skipped_others_used(monkeypatch):
    from reuploader import pipeline

    def list_shorts(url, n):
        if "tiktok" in url:
            raise source.TikTokIdError("TikTok не отдаёт список")
        return [{"id": "yt1", "title": "a", "view_count": 5, "url": "u/yt1", "duration": 30}]
    monkeypatch.setattr(source, "list_shorts", list_shorts)
    got = pipeline.pick(["https://www.tiktok.com/@x", "https://www.youtube.com/@y"], set(), strategy="rotate")
    assert got[0]["id"] == "yt1" and "https://www.tiktok.com/@x" in pipeline.pick.errors
    got = pipeline.pick(["https://www.tiktok.com/@x", "https://www.youtube.com/@y"], set(), strategy="top")
    assert got[0]["id"] == "yt1"
    with pytest.raises(source.TikTokIdError):         # единственный источник сломан — честная ошибка
        pipeline.pick(["https://www.tiktok.com/@x"], set())
