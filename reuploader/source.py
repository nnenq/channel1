"""Получение списка шортсов канала и скачивание через yt-dlp."""
import re
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp

from .ffmpeg_path import ffmpeg_exe


def _shorts_url(url):
    url = url.rstrip("/")
    if url.endswith("/shorts"):
        return url
    if "/@" in url or "/channel/" in url or "/c/" in url:
        return url + "/shorts"
    return url


TIKTOK_RE = re.compile(r"tiktok\.com/@([\w.\-]+)", re.I)


def is_tiktok(url):
    return bool(TIKTOK_RE.search(url or ""))


def is_youtube_id(video_id):
    return len(video_id) == 11 and not video_id.isdigit()


def _iso_from_ts(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


TIKTOK_VIDEO_RE = re.compile(r"tiktok\.com/@([\w.\-]+)/video/(\d+)", re.I)
SECUID_RE = re.compile(r'"secUid"\s*:\s*"(MS4wLjABAAAA[\w-]+)"')
TIKTOK_IDS_FILE = Path("data") / "tiktok_ids.json"   # ник -> secUid (ID аккаунта TikTok)


class TikTokIdError(Exception):
    """TikTok не отдал ID аккаунта — нужна ссылка на любое видео этого аккаунта."""


def _tiktok_ids():
    import json

    try:
        return json.loads(TIKTOK_IDS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def remember_tiktok_id(handle, sec_uid):
    import json

    ids = _tiktok_ids()
    ids[handle.lower()] = sec_uid
    TIKTOK_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TIKTOK_IDS_FILE.write_text(json.dumps(ids, ensure_ascii=False, indent=1), encoding="utf-8")


def tiktok_id_from_video(video_url):
    """ID аккаунта (secUid) и ник — из любого видео аккаунта (это TikTok отдаёт охотнее)."""
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        info = ydl.extract_info(video_url, download=False)
    sec_uid = info.get("channel_id")
    handle = info.get("uploader") or (TIKTOK_VIDEO_RE.search(video_url) or [None, None])[1]
    if not sec_uid or not handle:
        raise TikTokIdError("в видео нет ID аккаунта")
    remember_tiktok_id(handle, sec_uid)
    return handle, sec_uid


def _tiktok_id_from_profile_page(handle):
    """Пробуем достать secUid со страницы профиля, притворяясь браузером (curl_cffi)."""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return None
    try:
        html = creq.get(f"https://www.tiktok.com/@{handle}", impersonate="chrome", timeout=20).text
    except Exception:  # noqa: BLE001
        return None
    m = SECUID_RE.search(html)
    return m.group(1) if m else None


def list_tiktok(profile_url, scan_limit=200):
    """Ролики TikTok-аккаунта с просмотрами, датой и длительностью (через yt-dlp)."""
    handle = TIKTOK_RE.search(profile_url).group(1)
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": scan_limit}
    known = _tiktok_ids().get(handle.lower())
    urls = ([f"tiktokuser:{known}"] if known else []) + [f"https://www.tiktok.com/@{handle}"]
    info, last_err = None, None
    with yt_dlp.YoutubeDL(opts) as ydl:
        for url in urls:
            try:
                info = ydl.extract_info(url, download=False)
                break
            except yt_dlp.utils.DownloadError as e:
                last_err = e
        if info is None and "secondary user ID" in str(last_err):
            # TikTok спрятал ID аккаунта — пробуем достать его сами со страницы профиля
            sec_uid = _tiktok_id_from_profile_page(handle)
            if sec_uid:
                remember_tiktok_id(handle, sec_uid)
                info = ydl.extract_info(f"tiktokuser:{sec_uid}", download=False)
    if info is None:
        if "secondary user ID" in str(last_err):
            raise TikTokIdError(
                f"TikTok не отдаёт список роликов @{handle}. Добавь в источники ссылку на ЛЮБОЕ видео "
                f"этого аккаунта (https://www.tiktok.com/@{handle}/video/…) — бот возьмёт из него ID.")
        raise last_err

    videos = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        videos.append({
            "id": str(e["id"]),
            "title": e.get("title") or e.get("description") or "",
            "view_count": e.get("view_count"),
            "duration": e.get("duration"),
            "published": _iso_from_ts(e.get("timestamp")),
            "url": e.get("webpage_url") or f"https://www.tiktok.com/@{handle}/video/{e['id']}",
            "exact": True,   # TikTok отдаёт точное число просмотров
        })

    # Если в списке нет просмотров/дат — дочитываем первые ролики по одному
    missing = [v for v in videos if v["view_count"] is None or not v["published"]][:40]
    if missing:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            for v in missing:
                try:
                    full = ydl.extract_info(v["url"], download=False)
                except yt_dlp.utils.DownloadError:
                    continue
                v["view_count"] = full.get("view_count", v["view_count"])
                v["duration"] = full.get("duration") or v["duration"]
                v["published"] = v["published"] or _iso_from_ts(full.get("timestamp"))
                v["title"] = v["title"] or full.get("title") or ""
    add_age(videos)
    videos.sort(key=lambda v: v["view_count"] or 0, reverse=True)
    return videos


def list_shorts(channel_url, scan_limit=200):
    """Возвращает шортсы канала (YouTube или TikTok), отсортированные по просмотрам."""
    if is_tiktok(channel_url):
        return list_tiktok(channel_url, scan_limit)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": scan_limit,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(_shorts_url(channel_url), download=False)

    videos = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        videos.append(
            {
                "id": e["id"],
                "title": e.get("title") or "",
                "view_count": e.get("view_count"),
                "duration": e.get("duration"),
                "url": f"https://www.youtube.com/shorts/{e['id']}",
            }
        )

    # Иногда в плоском списке нет просмотров — тогда дочитываем по одному видео.
    if videos and all(v["view_count"] is None for v in videos):
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            for v in videos:
                try:
                    full = ydl.extract_info(v["url"], download=False)
                    v["view_count"] = full.get("view_count")
                except yt_dlp.utils.DownloadError:
                    pass

    videos.sort(key=lambda v: v["view_count"] or 0, reverse=True)
    return videos


def iso_duration(s):
    """'PT1M5S' -> 65 (секунд)."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m or not s:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


def enrich(videos, youtube=None, limit=60):
    """Добавляет дату выхода, возраст и просмотры в день.

    С клиентом YouTube API — точно и быстро (1 единица квоты на 50 роликов).
    Без него — через yt-dlp по одному ролику (медленно), только первые `limit`.
    Добавляемые ключи: published (ISO), age_days, views_per_day, duration (сек).
    """
    if youtube is not None:
        yt_videos = [v for v in videos if is_youtube_id(v["id"])]
        for i in range(0, len(yt_videos), 50):
            batch = yt_videos[i:i + 50]
            resp = youtube.videos().list(
                part="snippet,statistics,contentDetails", id=",".join(v["id"] for v in batch), maxResults=50
            ).execute()
            by_id = {it["id"]: it for it in resp.get("items", [])}
            for v in batch:
                it = by_id.get(v["id"])
                if not it:
                    continue
                v["published"] = it["snippet"].get("publishedAt")
                v["title"] = it["snippet"].get("title") or v["title"]
                v["duration"] = iso_duration(it.get("contentDetails", {}).get("duration")) or v.get("duration")
                views = it.get("statistics", {}).get("viewCount")
                if views is not None:
                    v["view_count"] = int(views)
                    v["exact"] = True
    else:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            for v in videos[:limit]:
                if v.get("published"):
                    continue
                try:
                    info = ydl.extract_info(v["url"], download=False)
                except yt_dlp.utils.DownloadError:
                    continue
                ts = info.get("timestamp")
                if ts:
                    v["published"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
                elif info.get("upload_date"):
                    d = info["upload_date"]
                    v["published"] = f"{d[:4]}-{d[4:6]}-{d[6:]}T00:00:00+00:00"
                if info.get("view_count") is not None:
                    v["view_count"] = info["view_count"]
                v["duration"] = info.get("duration") or v.get("duration")

    add_age(videos)
    return videos


def add_age(videos):
    """age_days и views_per_day по дате выхода."""
    now = datetime.now(timezone.utc)
    for v in videos:
        if v.get("published"):
            dt = datetime.fromisoformat(v["published"].replace("Z", "+00:00"))
            v["age_days"] = round(max((now - dt).total_seconds(), 0) / 86400, 2)
            # Моложе суток считаем как сутки, чтобы свежие ролики не "взрывали" рейтинг
            v["views_per_day"] = int((v["view_count"] or 0) / max(v["age_days"], 1))
    return videos


def download(video_url, out_dir):
    """Скачивает видео в лучшем качестве, возвращает (путь, метаданные)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / "%(id)s.src.%(ext)s"),
        "ffmpeg_location": ffmpeg_exe(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    meta = {
        "id": info["id"],
        "title": info.get("title") or "",
        "description": info.get("description") or "",
        "tags": info.get("tags") or [],
        "view_count": info.get("view_count"),
        "duration": info.get("duration"),
        "published": (datetime.fromtimestamp(info["timestamp"], timezone.utc).isoformat()
                      if info.get("timestamp") else None),
    }
    return path, meta
