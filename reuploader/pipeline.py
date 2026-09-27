"""Общая логика: выбрать видео, скачать, уникализировать, собрать метаданные.

Используется и CLI (`python -m reuploader run`), и телеграм-ботом.
"""
from pathlib import Path

DEFAULT_TEXT = {
    "title_template": "{title}",
    "description_template": "{description}\n\n#shorts",
    "keep_tags": True,
    "extra_tags": ["shorts"],
}


SORTS = {
    "views": lambda v: v["view_count"] or 0,                  # больше всего просмотров всего
    "per_day": lambda v: v.get("views_per_day") or -1,        # быстрее всего набирают
}


def rank(videos, exclude_ids=(), sort_by="views", max_age_days=0, min_views=0):
    """Фильтрует и сортирует ролики.

    max_age_days > 0 — только ролики не старше стольких дней
    (ролики без известной даты при этом отбрасываются).
    """
    out = []
    for v in videos:
        if v["id"] in exclude_ids or (v["view_count"] or 0) < min_views:
            continue
        if max_age_days and (v.get("age_days") is None or v["age_days"] > max_age_days):
            continue
        out.append(v)
    out.sort(key=SORTS.get(sort_by, SORTS["views"]), reverse=True)
    return out


def pick(sources, exclude_ids, count=1, scan_limit=200, min_views=0, strategy="top",
         sort_by="views", max_age_days=0, enrich=None):
    """Выбирает `count` лучших ещё не перезалитых шортсов.

    strategy="top"    — общий рейтинг по всем источникам;
    strategy="rotate" — источники по очереди (в порядке списка `sources`),
                        с каждого берётся его лучший.
    sort_by="views" — по просмотрам всего, "per_day" — по просмотрам в день.
    enrich(videos) — добавляет даты/просмотры в день (см. source.enrich);
    нужен для sort_by="per_day" и max_age_days.
    Возвращает список словарей с ключами id, title, view_count, url, source, ...
    """
    from .source import enrich as ytdlp_enrich
    from .source import list_shorts

    need_dates = sort_by == "per_day" or bool(max_age_days)

    def load(src):
        videos = [dict(v, source=src) for v in list_shorts(src, scan_limit)]
        if enrich:
            enrich(videos)
        elif need_dates:
            ytdlp_enrich(videos)
        return videos

    ranked = {}
    opts = dict(sort_by=sort_by, max_age_days=max_age_days, min_views=min_views)
    if strategy == "rotate":
        picked, seen = [], set()
        while len(picked) < count:
            progress = False
            for src in sources:
                if len(picked) >= count:
                    break
                if src not in ranked:
                    ranked[src] = rank(load(src), **opts)
                for v in ranked[src]:
                    if v["id"] not in seen and v["id"] not in exclude_ids:
                        picked.append(v)
                        seen.add(v["id"])
                        progress = True
                        break
            if not progress:
                break
        return picked

    pool = []
    for src in sources:
        pool += load(src)
    return rank([v for v in pool if v["id"] not in exclude_ids], **opts)[:count]


def prepare(video_url, work_dir, effects):
    """Скачивает видео и применяет эффекты. Возвращает (src, out, meta)."""
    from .effects import apply_effects
    from .source import download

    src, meta = download(video_url, work_dir)
    out = Path(work_dir) / f"{meta['id']}.out.mp4"
    apply_effects(src, out, effects)
    return src, out, meta


def build_text(meta, opts=None):
    """Название, описание и теги для новой публикации."""
    o = dict(DEFAULT_TEXT, **(opts or {}))
    fmt = lambda t: t.format(title=meta["title"], description=meta["description"]).strip()
    title = fmt(o["title_template"]) or meta["title"] or "#shorts"
    description = fmt(o["description_template"])
    tags = (meta["tags"] if o["keep_tags"] else []) + list(o["extra_tags"])
    return title, description, tags
