"""Одна публикация: выбрать видео -> скачать -> уникализировать -> залить.

Работает синхронно (yt-dlp, ffmpeg и загрузка блокирующие), запускается
из планировщика в отдельном потоке.
"""
import re
import shutil
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field

from ..pipeline import build_text, pick, prepare
from ..source import enrich
from . import trends

SHORT_ID = re.compile(r"(?:shorts/|v=|youtu\.be/)([\w-]{11})")


@dataclass
class Result:
    status: str                     # done | failed | skipped
    info: str = ""
    title: str = ""
    new_id: str | None = None
    exhausted: bool = False         # новых видео на каналах-источниках не осталось
    auth_problem: bool = False
    extra: dict = field(default_factory=dict)


def fmt_views(n):
    return f"{n / 1e6:g}M" if n >= 1e6 else f"{n / 1e3:g}K" if n >= 1e3 else str(n)


def video_id(url):
    m = SHORT_ID.search(url or "")
    return m.group(1) if m else None


def _error_text(e):
    try:
        from googleapiclient.errors import HttpError

        if isinstance(e, HttpError):
            reason = e.error_details[0].get("reason") if e.error_details else ""
            if reason in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded"):
                return f"YouTube: превышен лимит ({reason}). Попробую в следующий раз."
            return f"YouTube API {e.status_code}: {e.reason}"
    except ImportError:
        pass
    return f"{type(e).__name__}: {e}"


TG_MAX_MB = 48
FIT_REFRESH = timedelta(hours=24)


def fit_target_for(db, project):
    """Целевая длина для подгонки: своя или «как на канале» (медиана лучших 30% по просмотрам).

    «Как на канале» — по твоему каналу для перезалива; если на нём пока мало роликов,
    по каналам-источникам. Считается раз в сутки и кэшируется в проекте.
    «Диапазон» — итог где-то между fit_min и fit_max. Возвращает (цель, допуск) или None."""
    mode = project.get("fit_mode") or "off"
    if mode == "fixed":
        return (project["fit_seconds"], 0.05) if project["fit_seconds"] else None
    if mode == "range":
        from ..smartcut.target import fit_params

        lo, hi = project.get("fit_min") or 0, project.get("fit_max") or 0
        return fit_params(lo, hi) if hi else None
    if mode != "channel":
        return None
    cached_at = project.get("fit_cached_at")
    if project.get("fit_cached") and cached_at and \
            datetime.now(timezone.utc) - datetime.fromisoformat(cached_at) < FIT_REFRESH:
        return project["fit_cached"], 0.05
    from ..smartcut.target import target_from_videos
    from ..source import list_shorts

    target = None
    try:
        if project.get("channel_id"):
            vids = list_shorts(f"https://www.youtube.com/channel/{project['channel_id']}", 100)
            with_views = [(v.get("duration"), v.get("view_count")) for v in vids if v.get("view_count")]
            if len(with_views) >= 5:
                target, _ = target_from_videos(with_views)
        if not target:
            pool = []
            for src in db.sources_in_rotation_order(project["id"]):
                pool += [(v.get("duration"), v.get("view_count")) for v in list_shorts(src, 100)]
            target, _ = target_from_videos(pool)
    except Exception:  # noqa: BLE001 — не узнали длину: берём прошлую или «своя длина»
        target = None
    if target:
        db.update_project(project["id"], fit_cached=target,
                          fit_cached_at=datetime.now(timezone.utc).isoformat())
        return target, 0.05
    fallback = project.get("fit_cached") or project["fit_seconds"]
    return (fallback, 0.05) if fallback else None


def run_slot(db, settings, slot):
    """Выполняет слот. delivery проекта:
    youtube  — залить на YouTube;
    telegram — только прислать обработанное видео владельцу в Telegram;
    both     — и то, и другое.
    Для telegram/both файл не удаляется: его путь в result.extra["file"],
    папку result.extra["work"] удаляет планировщик после отправки.
    """
    from ..effects import shrink_to
    from ..uploader import AuthError, upload, youtube_client
    from .db import needs_youtube

    project = db.project(slot["project_id"])
    if not project:
        return Result("skipped", "проект удалён")
    to_youtube = needs_youtube(project)
    to_telegram = project["delivery"] in ("telegram", "both")

    youtube = None
    if project["token_path"]:
        try:
            youtube = youtube_client(project["token_path"])
        except AuthError as e:
            if to_youtube:
                return Result("failed", str(e), auth_problem=True)
    elif to_youtube:
        return Result("failed", "не привязан канал для перезалива", auth_problem=True)

    # 1. Какое видео берём
    source_url = None
    info = {}
    skipped_sources = {}
    pick_note = ""
    if slot["video_url"]:
        url = slot["video_url"]
        title_hint = slot.get("video_title") or ""
        views = None
    else:
        sources = db.sources_in_rotation_order(project["id"])
        if not sources:
            return Result("failed", "в проекте нет каналов-источников")
        min_views = project.get("min_views") or 0
        common = dict(count=1, strategy=project["strategy"], min_views=min_views,
                      min_duration=project["min_duration"], max_duration=project["max_duration"],
                      enrich=(lambda vs: enrich(vs, youtube)) if youtube else None)
        errors = {}
        try:
            # 1) свежие (не старше N дней) и набравшие порог просмотров
            picked = pick(sources, db.uploaded_ids(project["id"]), sort_by=project["sort_by"],
                          max_age_days=project["max_age_days"], trend=trends.hook(db), **common)
            errors.update(getattr(pick, "errors", {}))
            # 2) свежих выше порога нет — старые, но популярные (по просмотрам за всё время)
            if not picked and project["max_age_days"] and project.get("fallback_old"):
                picked = pick(sources, db.uploaded_ids(project["id"]), sort_by="views",
                              max_age_days=0, **common)
                errors.update(getattr(pick, "errors", {}))
                if picked:
                    fresh = f" от {fmt_views(min_views)} просмотров" if min_views else ""
                    pick_note = (f"свежих роликов (до {project['max_age_days']} дн.){fresh} не нашлось — "
                                 f"взял старый популярный")
        except Exception as e:  # noqa: BLE001
            return Result("failed", "не удалось получить список видео: " + _error_text(e))
        if not picked:
            why = f" за последние {project['max_age_days']} дн." if project["max_age_days"] else ""
            if project["max_age_days"] and project.get("fallback_old"):
                why = " (ни свежих, ни старых)"
            if min_views:
                why += f" от {fmt_views(min_views)} просмотров"
            if project["min_duration"] or project["max_duration"]:
                why += " подходящей длины"
            return Result("skipped", f"новых видео{why} нет — всё подходящее уже перезалито", exhausted=True)
        info = picked[0]
        url, title_hint, views, source_url = info["url"], info["title"], info["view_count"], info["source"]
        skipped_sources = {src: str(e).splitlines()[0][:200] for src, e in errors.items()}

    # 2. Скачать + (подогнать длину) + уникализировать + (залить)
    work = settings.work_dir / f"p{project['id']}_s{slot['id']}"
    keep = False
    fit = fit_target_for(db, project)
    fit_target, fit_tol = fit if fit else (None, 0.05)
    transcriber = None
    if fit_target:
        from functools import partial

        from ..smartcut.analyze import whisper_transcribe

        transcriber = partial(whisper_transcribe, model_size=settings.whisper_model)
    try:
        _, out, meta = prepare(url, work, project["effects"], fit_target, transcriber, fit_tol)
        title, description, tags = build_text(meta)
        title = slot.get("publication_title") or title
        new_id = None
        publish_at = None
        warning = ""
        cover_path = slot.get("cover_path")
        cover_status = None
        recorded = False
        if cover_path and not Path(cover_path).is_file():
            raise ValueError("Выбранная обложка потеряна. Подготовь публикацию заново.")
        if not cover_path and slot.get("cover_choice", "project") != "off" and project.get("cover_mode") == "auto":
            try:
                from .. import covers
                frames = covers.extract_frames(out, work / "cover_frames")
                cover_path = str(settings.data_dir / "covers" / "selected" / f"slot{slot['id']}.jpg")
                covers.render(frames[0], covers.suggested_text(title), project.get("cover_style", "lemon"), cover_path)
                db.x("UPDATE slots SET cover_path=? WHERE id=?", cover_path, slot["id"])
            except Exception:
                cover_path = None
                warning = "Не получилось создать автообложку. Видео опубликовано без неё."
        if to_youtube:
            privacy = project["privacy"]
            if privacy == "scheduled":
                run_at = datetime.fromisoformat(slot["run_at"])
                if run_at - datetime.now(timezone.utc) >= timedelta(minutes=10):
                    publish_at = run_at
                privacy = "public"      # время уже подошло — публикуем сразу
            new_id = upload(youtube, out, title, description, tags, privacy, "24", False,
                            publish_at=publish_at)
            # Persist successful upload before optional thumbnail and Telegram operations.
            try:
                db.add_upload(project["id"], source_url, meta["id"], title,
                              views if views is not None else meta.get("view_count"), new_id,
                              info.get("published") or meta.get("published"))
                recorded = True
            except Exception:
                warning += " Видео загружено, но запись истории не удалась. Проверь канал перед новой публикацией."
            if cover_path:
                try:
                    from ..uploader import set_thumbnail
                    set_thumbnail(youtube, new_id, cover_path)
                    cover_status = "set"
                except Exception:
                    cover_status = "manual"
                    warning += " YouTube не принял обложку. Установи присланный JPG вручную в Studio."
        if to_telegram:
            try:
                shrink_to(out, TG_MAX_MB, meta.get("duration"))
                keep = True
            except Exception:
                if not new_id:
                    raise
                warning += " Видео на YouTube, но подготовка копии для Telegram не удалась."
    except AuthError as e:
        return Result("failed", str(e), title=title_hint, auth_problem=True)
    except Exception as e:  # noqa: BLE001 — любую ошибку показываем пользователю
        return Result("failed", _error_text(e), title=title_hint)
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)

    if not info.get("published") and meta.get("published"):
        info = dict(info, published=meta["published"], duration=meta.get("duration"))
    views = views if views is not None else meta.get("view_count")
    if not recorded and not new_id:
        db.add_upload(project["id"], source_url, meta["id"], title, views, "", info.get("published"))
    extra = {"views": views, "published": info.get("published"),
             "duration": info.get("duration") or meta.get("duration"),
             "views_per_day": info.get("views_per_day"),
             "trend_per_day": info.get("trend_per_day"), "hot": info.get("hot"),
             "description": description, "tags": tags, "source_url": url, "fit": meta.get("fit"),
            "skipped_sources": skipped_sources, "pick_note": pick_note,
            "cover": cover_path, "cover_status": cover_status, "warning": warning.strip()}
    if keep:
        extra.update(file=str(out), work=str(work))
    link = f"https://youtube.com/shorts/{new_id}" if new_id else "отправлено тебе в Telegram"
    extra["publish_at"] = publish_at
    return Result("done", link, title=title, new_id=new_id, extra=extra)
