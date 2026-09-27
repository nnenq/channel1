"""Одна публикация: выбрать видео -> скачать -> уникализировать -> залить.

Работает синхронно (yt-dlp, ffmpeg и загрузка блокирующие), запускается
из планировщика в отдельном потоке.
"""
import re
import shutil
from dataclasses import dataclass, field

from ..pipeline import build_text, pick, prepare

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


def run_slot(db, settings, slot):
    from ..uploader import AuthError, upload, youtube_client

    project = db.project(slot["project_id"])
    if not project:
        return Result("skipped", "проект удалён")
    if not project["token_path"]:
        return Result("failed", "не привязан канал для перезалива", auth_problem=True)

    # 1. Какое видео заливаем
    source_url = None
    if slot["video_url"]:
        url = slot["video_url"]
        title_hint = slot.get("video_title") or ""
        views = None
    else:
        sources = db.sources_in_rotation_order(project["id"])
        if not sources:
            return Result("failed", "в проекте нет каналов-источников")
        picked = pick(sources, db.uploaded_ids(project["id"]), count=1, strategy=project["strategy"])
        if not picked:
            return Result("skipped", "новых видео нет — всё уже перезалито", exhausted=True)
        v = picked[0]
        url, title_hint, views, source_url = v["url"], v["title"], v["view_count"], v["source"]

    # 2. Скачать + уникализировать + залить
    work = settings.work_dir / f"p{project['id']}_s{slot['id']}"
    try:
        youtube = youtube_client(project["token_path"])
        _, out, meta = prepare(url, work, project["effects"])
        title, description, tags = build_text(meta)
        new_id = upload(youtube, out, title, description, tags,
                        project["privacy"], "24", False)
    except AuthError as e:
        return Result("failed", str(e), title=title_hint, auth_problem=True)
    except Exception as e:  # noqa: BLE001 — любую ошибку показываем пользователю
        return Result("failed", _error_text(e), title=title_hint)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    db.add_upload(project["id"], source_url, meta["id"], meta["title"],
                  views if views is not None else meta.get("view_count"), new_id)
    return Result("done", f"https://youtube.com/shorts/{new_id}", title=meta["title"], new_id=new_id)
