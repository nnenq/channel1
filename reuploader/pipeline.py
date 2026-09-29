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
    "per_day": lambda v: v.get("views_per_day") or -1,        # в среднем в день за всё время
}

TREND_MIN_PER_DAY = 300     # меньше этого прироста в сутки ролик "в тренде" не считается
TREND_SHARE = 0.05          # ...и меньше 5% от самого быстрорастущего в выборке
FRESH_DAYS = 7              # для молодых роликов без замеров берём средние просмотры в день


def current_rate(v):
    """Сколько просмотров в сутки ролик набирает сейчас.

    trend_per_day — по замерам (прирост за последние ~сутки);
    если замеров ещё нет, для свежих роликов — средние просмотры в день.
    """
    if v.get("trend_per_day") is not None:
        return v["trend_per_day"]
    if v.get("age_days") is not None and v["age_days"] <= FRESH_DAYS:
        return v.get("views_per_day")
    return None


def _trend_sorted(videos):
    """Сначала то, что растёт прямо сейчас (по приросту), потом остальное по просмотрам."""
    rates = [r for r in (current_rate(v) for v in videos) if r]
    threshold = max(TREND_MIN_PER_DAY, TREND_SHARE * max(rates)) if rates else float("inf")

    def key(v):
        r = current_rate(v) or 0
        return (1, r) if r >= threshold else (0, v["view_count"] or 0)

    for v in videos:
        v["hot"] = (current_rate(v) or 0) >= threshold
    return sorted(videos, key=key, reverse=True)


def rank(videos, exclude_ids=(), sort_by="views", max_age_days=0, min_views=0,
         min_duration=0, max_duration=0, topic_terms=()):
    """Фильтрует и сортирует ролики.

    max_age_days > 0 — только ролики не старше стольких дней
    (ролики без известной даты при этом отбрасываются).
    min_duration / max_duration (сек, 0 — без ограничения) — фильтр по длине ролика
    (ролики с неизвестной длиной при включённом фильтре отбрасываются).
    topic_terms — только ролики про эту тему (см. topics.expand / topics.matches).
    """
    from .topics import matches

    out = []
    for v in videos:
        if v["id"] in exclude_ids or (v["view_count"] or 0) < min_views:
            continue
        if topic_terms and not matches(v, topic_terms):
            continue
        if max_age_days and (v.get("age_days") is None or v["age_days"] > max_age_days):
            continue
        dur = v.get("duration")
        if (min_duration or max_duration) and not dur:
            continue
        if (min_duration and dur < min_duration) or (max_duration and dur > max_duration):
            continue
        out.append(v)
    if sort_by == "trend":
        return _trend_sorted(out)
    out.sort(key=SORTS.get(sort_by, SORTS["views"]), reverse=True)
    return out


def pick(sources, exclude_ids, count=1, scan_limit=200, min_views=0, strategy="top",
         sort_by="views", max_age_days=0, enrich=None, trend=None, min_duration=0, max_duration=0,
         topic_terms=()):
    """Выбирает `count` лучших ещё не перезалитых шортсов.

    strategy="top"    — общий рейтинг по всем источникам;
    strategy="rotate" — источники по очереди (в порядке списка `sources`),
                        с каждого берётся его лучший.
    sort_by="trend" — сначала то, что набирает просмотры прямо сейчас, потом остальное
    по просмотрам; "views" — по просмотрам всего; "per_day" — в среднем в день.
    enrich(videos) — добавляет даты/просмотры в день (см. source.enrich);
    trend(videos, source) — записывает замер просмотров и добавляет trend_per_day.
    Нечитаемый источник пропускается (ошибка сохраняется в pick.errors); исключение —
    только если не прочитался ни один источник.
    Возвращает список словарей с ключами id, title, view_count, url, source, ...
    """
    from .source import enrich as ytdlp_enrich
    from .source import list_shorts

    # теги/описание для темы тоже приходят вместе с датами (enrich)
    need_dates = sort_by in ("per_day", "trend") or bool(max_age_days or min_duration or max_duration or topic_terms)

    errors = {}

    def load(src):
        try:
            videos = [dict(v, source=src) for v in list_shorts(src, scan_limit)]
        except Exception as e:  # noqa: BLE001 — один сломанный источник не мешает остальным
            errors[src] = e
            if len(errors) == len(sources):
                raise
            return []
        if enrich:
            enrich(videos)
        elif need_dates:
            ytdlp_enrich(videos)
        if trend:
            trend(videos, src)
        return videos

    ranked = {}
    opts = dict(sort_by=sort_by, max_age_days=max_age_days, min_views=min_views,
                min_duration=min_duration, max_duration=max_duration, topic_terms=tuple(topic_terms))
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
        pick.errors = errors
        return picked

    pool = []
    for src in sources:
        pool += load(src)
    pick.errors = errors
    return rank([v for v in pool if v["id"] not in exclude_ids], **opts)[:count]


def prepare(video_url, work_dir, effects, fit_target=None, transcriber=None, fit_tolerance=0.05):
    """Скачивает видео, при необходимости подгоняет длину (умная обрезка) и применяет эффекты.

    fit_target — целевая длина, сек (None — не резать). Результат подгонки — в meta["fit"]:
    отчёт smart_cut или {"status": "error", ...}; при ошибке заливаем без обрезки.
    Возвращает (src, out, meta)."""
    from .effects import apply_effects, randomize
    from .source import download

    work = Path(work_dir)
    src, meta = download(video_url, work)
    fx_input = src
    meta["fit"] = None
    if fit_target:
        from .smartcut import smart_cut

        cut = work / f"{meta['id']}.fit.mp4"
        try:
            report = smart_cut(src, cut, fit_target, tolerance=fit_tolerance, transcriber=transcriber,
                               work_dir=work / "fit_tmp")
            meta["fit"] = report
            if report["status"] != "already_short" and cut.exists():
                fx_input = cut
        except Exception as e:  # noqa: BLE001 — не получилось обрезать: заливаем как есть
            meta["fit"] = {"status": "error", "error": f"{type(e).__name__}: {e}"[:300]}
    out = work / f"{meta['id']}.out.mp4"
    fx = randomize(effects)                       # у каждого ролика свои параметры
    meta["fx"] = fx
    subs = None
    meta["subs"] = None
    if effects.get("subtitles") and transcriber:
        from .smartcut.media import probe
        from .subtitles import to_ass

        try:
            info = probe(fx_input)
            subs = to_ass(transcriber(fx_input), info.width, info.height, work / "subs.ass")
            meta["subs"] = "ok" if subs else "no_speech"
        except Exception as e:  # noqa: BLE001 — без субтитров, но ролик всё равно выйдет
            meta["subs"] = f"error: {type(e).__name__}: {e}"[:200]
    try:
        apply_effects(fx_input, out, fx, subs)
    except Exception:
        if not subs:
            raise
        meta["subs"] = "error: не удалось вшить субтитры"
        apply_effects(fx_input, out, fx)
    return src, out, meta


def build_text(meta, opts=None):
    """Название, описание и теги для новой публикации."""
    o = dict(DEFAULT_TEXT, **(opts or {}))
    fmt = lambda t: t.format(title=meta["title"], description=meta["description"]).strip()
    title = fmt(o["title_template"]) or meta["title"] or "#shorts"
    description = fmt(o["description_template"])
    tags = (meta["tags"] if o["keep_tags"] else []) + list(o["extra_tags"])
    return title, description, tags
