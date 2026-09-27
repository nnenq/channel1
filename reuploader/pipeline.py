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


def pick(sources, exclude_ids, count=1, scan_limit=200, min_views=0, strategy="top"):
    """Выбирает `count` самых просматриваемых ещё не перезалитых шортсов.

    strategy="top"    — общий топ по всем источникам (крупный канал будет чаще);
    strategy="rotate" — источники по очереди (в порядке списка `sources`),
                        с каждого берётся его топ.
    Возвращает список словарей с ключами id, title, view_count, url, source.
    """
    from .source import list_shorts

    def fresh(videos):
        return [
            v for v in videos
            if v["id"] not in exclude_ids and (v["view_count"] or 0) >= min_views
        ]

    if strategy == "rotate":
        picked, seen = [], set()
        per_source = {}
        while len(picked) < count:
            progress = False
            for src in sources:
                if len(picked) >= count:
                    break
                if src not in per_source:
                    per_source[src] = [dict(v, source=src) for v in list_shorts(src, scan_limit)]
                for v in per_source[src]:
                    if v["id"] not in seen and v["id"] not in exclude_ids and (v["view_count"] or 0) >= min_views:
                        picked.append(v)
                        seen.add(v["id"])
                        progress = True
                        break
            if not progress:
                break
        return picked

    pool = []
    for src in sources:
        pool += [dict(v, source=src) for v in list_shorts(src, scan_limit)]
    pool.sort(key=lambda v: v["view_count"] or 0, reverse=True)
    return fresh(pool)[:count]


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
