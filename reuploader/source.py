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


def list_shorts(channel_url, scan_limit=200):
    """Возвращает шортсы канала, отсортированные по просмотрам (по убыванию)."""
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
        for i in range(0, len(videos), 50):
            batch = videos[i:i + 50]
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
