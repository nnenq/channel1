"""Получение списка шортсов канала и скачивание через yt-dlp."""
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
    }
    return path, meta
