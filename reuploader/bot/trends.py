"""Что набирает просмотры прямо сейчас: по замерам просмотров раз в пару часов.

Прирост считается за последние ~сутки: берём замер ~24 ч назад (или самый
ранний за последние 2 суток, но не моложе 3 ч) и пересчитываем на сутки.
"""
import logging
from datetime import timedelta

from .db import from_iso, iso, utcnow

log = logging.getLogger("trends")
SNAPSHOT_EVERY = timedelta(hours=2)     # как часто замерять каналы
MIN_SNAPSHOT_GAP = timedelta(minutes=50)
KEEP = timedelta(days=7)
TARGET_WINDOW = timedelta(hours=24)
MIN_WINDOW = timedelta(hours=3)
MAX_WINDOW = timedelta(hours=48)


def record(db, videos, channel, force=False):
    """Сохраняет замер просмотров (не чаще раза в ~час на канал)."""
    now = utcnow()
    last = db.last_snapshot_at(channel)
    if force or not last or now - last >= MIN_SNAPSHOT_GAP:
        db.add_snapshots(channel, videos, iso(now))


def apply(db, videos):
    """Добавляет каждому ролику trend_per_day — прирост просмотров за сутки (или None)."""
    now = utcnow()
    for v in videos:
        v["trend_per_day"] = None
        if v.get("view_count") is None:
            continue
        exact = bool(v.get("exact"))   # точные (API) и примерные ("1.2M") замеры не смешиваем
        old = db.snapshot_before(v["id"], exact, iso(now - TARGET_WINDOW), iso(now - MAX_WINDOW))
        if not old:
            old = db.snapshot_oldest_after(v["id"], exact, iso(now - MAX_WINDOW), iso(now - MIN_WINDOW))
        if not old:
            continue
        hours = (now - from_iso(old["at"])).total_seconds() / 3600
        v["trend_per_day"] = max(0, int((v["view_count"] - old["views"]) * 24 / hours))
    return videos


def hook(db):
    """Колбэк для pipeline.pick(trend=...): замер + расчёт прироста."""
    def run(videos, channel):
        record(db, videos, channel)
        apply(db, videos)
    return run


def snapshot_all(db, list_shorts, enrich, youtube_client):
    """Периодический замер всех каналов-источников (вызывается из планировщика в потоке)."""
    now = utcnow()
    for row in db.all_source_channels():
        last = db.last_snapshot_at(row["url"])
        if last and now - last < SNAPSHOT_EVERY:
            continue
        try:
            videos = list_shorts(row["url"], 200)
        except Exception as e:  # noqa: BLE001 — один сломанный канал не должен ломать замер остальных
            log.warning("замер %s: %s", row["url"], str(e).splitlines()[0][:200])
            continue
        if row["token_path"]:
            try:
                enrich(videos, youtube_client(row["token_path"]))   # точные просмотры через API
            except Exception:  # noqa: BLE001 — хватит и примерных из списка канала
                pass
        record(db, videos, row["url"], force=True)
    db.prune_snapshots(iso(now - KEEP))
