"""Одна публикация: выбрать видео -> скачать -> уникализировать -> залить.

Работает синхронно (yt-dlp, ffmpeg и загрузка блокирующие), запускается
из планировщика в отдельном потоке.
"""
import re
import shutil
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
    if slot["video_url"]:
        url = slot["video_url"]
        title_hint = slot.get("video_title") or ""
        views = None
    else:
        sources = db.sources_in_rotation_order(project["id"])
        if not sources:
            return Result("failed", "в проекте нет каналов-источников")
        try:
            picked = pick(sources, db.uploaded_ids(project["id"]), count=1,
                          strategy=project["strategy"], sort_by=project["sort_by"],
                          max_age_days=project["max_age_days"],
                          min_duration=project["min_duration"], max_duration=project["max_duration"],
                          enrich=(lambda vs: enrich(vs, youtube)) if youtube else None,
                          trend=trends.hook(db))
        except Exception as e:  # noqa: BLE001
            return Result("failed", "не удалось получить список видео: " + _error_text(e))
        if not picked:
            why = f" за последние {project['max_age_days']} дн." if project["max_age_days"] else ""
            if project["min_duration"] or project["max_duration"]:
                why += " подходящей длины"
            return Result("skipped", f"новых видео{why} нет — всё уже перезалито", exhausted=True)
        info = picked[0]
        url, title_hint, views, source_url = info["url"], info["title"], info["view_count"], info["source"]

    # 2. Скачать + уникализировать + (залить)
    work = settings.work_dir / f"p{project['id']}_s{slot['id']}"
    keep = False
    try:
        _, out, meta = prepare(url, work, project["effects"])
        title, description, tags = build_text(meta)
        new_id = None
        publish_at = None
        if to_youtube:
            privacy = project["privacy"]
            if privacy == "scheduled":
                run_at = datetime.fromisoformat(slot["run_at"])
                if run_at - datetime.now(timezone.utc) >= timedelta(minutes=10):
                    publish_at = run_at
                privacy = "public"      # время уже подошло — публикуем сразу
            new_id = upload(youtube, out, title, description, tags, privacy, "24", False,
                            publish_at=publish_at)
        if to_telegram:
            shrink_to(out, TG_MAX_MB, meta.get("duration"))
            keep = True
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
    db.add_upload(project["id"], source_url, meta["id"], meta["title"], views, new_id or "",
                  info.get("published"))
    extra = {"views": views, "published": info.get("published"),
             "duration": info.get("duration") or meta.get("duration"),
             "views_per_day": info.get("views_per_day"),
             "trend_per_day": info.get("trend_per_day"), "hot": info.get("hot"),
             "description": description, "tags": tags, "source_url": url}
    if keep:
        extra.update(file=str(out), work=str(work))
    link = f"https://youtube.com/shorts/{new_id}" if new_id else "отправлено тебе в Telegram"
    extra["publish_at"] = publish_at
    return Result("done", link, title=meta["title"], new_id=new_id, extra=extra)
