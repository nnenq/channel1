"""Целевая длина «как на канале»: медиана длительности лучших по просмотрам роликов (верхние 30%)."""
import math
import re
from statistics import median

TOP_SHARE = 0.30


def target_from_videos(videos, top_share=TOP_SHARE):
    """videos: [(длительность_сек, просмотры)]. Возвращает (цель_сек, сколько роликов учтено) или (None, 0)."""
    vs = [(d, v) for d, v in videos if d and v is not None]
    if not vs:
        return None, 0
    vs.sort(key=lambda x: x[1], reverse=True)
    top = vs[: max(1, math.ceil(len(vs) * top_share))]
    return round(median(d for d, _ in top), 1), len(top)


def parse_duration(s):
    """'1:05' / '65' / '65s' / '1м5с' -> 65.0"""
    s = s.strip().lower().replace(",", ".")
    if ":" in s:
        parts = [float(p) for p in s.split(":")]
        sec = 0.0
        for p in parts:
            sec = sec * 60 + p
        return sec
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*(?:m|м|мин)\s*)?(?:(\d+(?:\.\d+)?)\s*(?:s|с|сек)?)?", s)
    if not m or not any(m.groups()):
        raise ValueError(f"не понял длительность: {s}")
    return float(m.group(1) or 0) * 60 + float(m.group(2) or 0)


def parse_views(s):
    """'120000' / '120k' / '1.2M' / '1,2 млн' -> int"""
    s = s.strip().lower().replace(" ", "").replace(",", ".")
    mult = 1
    for suf, k in (("млн", 1e6), ("m", 1e6), ("тыс", 1e3), ("k", 1e3), ("к", 1e3)):
        if s.endswith(suf):
            s, mult = s[: -len(suf)], k
            break
    return int(float(s) * mult)


def parse_list(text):
    """Строки «длительность просмотры», например «0:45 120k». Пустые строки пропускаются."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out.append((parse_duration(parts[0]), parse_views("".join(parts[1:]))))
    return out


def target_from_channel(url, scan_limit=100):
    """Ролики канала (YouTube / TikTok) с длительностью и просмотрами -> цель."""
    from ..source import enrich, list_shorts

    videos = list_shorts(url, scan_limit)
    if sum(1 for v in videos if v.get("duration")) < len(videos) // 2:
        enrich(videos, None, limit=40)      # длительности через yt-dlp, если их нет в списке
    return target_from_videos([(v.get("duration"), v.get("view_count")) for v in videos])
