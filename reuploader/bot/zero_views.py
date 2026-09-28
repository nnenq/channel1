"""Автоудаление: ролик, у которого через N часов после публикации 0 просмотров, удаляется с канала.

Включается в проекте (autodelete_zero). Проверяется каждый ролик один раз — в окне
[N ч; N+48 ч] после публикации; более старые ролики не трогаем. Приватные/по ссылке
не удаляем: у них 0 просмотров — это нормально.
"""
import logging

from .db import utcnow

log = logging.getLogger("zero_views")


def stats(youtube, ids):
    """{id: (views, privacy)} — чего нет в ответе, того уже нет на YouTube."""
    out = {}
    for i in range(0, len(ids), 50):
        resp = youtube.videos().list(part="statistics,status", id=",".join(ids[i:i + 50])).execute()
        for it in resp.get("items", []):
            out[it["id"]] = (int(it.get("statistics", {}).get("viewCount", 0)),
                             it.get("status", {}).get("privacyStatus"))
    return out


def run(db, youtube_client, delete_video, no_rights_exc):
    """Проверяет и удаляет. -> [(project, [названия удалённых], ошибка или None)]."""
    by_project = {}
    for row in db.zero_view_candidates(utcnow()):
        by_project.setdefault(row["project_id"], []).append(row)
    report = []
    for pid, rows in by_project.items():
        project = db.project(pid)
        if not project or not project["token_path"]:
            continue
        deleted, error = [], None
        try:
            yt = youtube_client(project["token_path"])
            st = stats(yt, [r["new_video_id"] for r in rows])
        except Exception as e:  # noqa: BLE001 — попробуем в следующий раз
            log.warning("%s: не смог проверить просмотры: %s", project["name"], e)
            continue
        for r in rows:
            views, privacy = st.get(r["new_video_id"], (None, None))
            if views is None:                     # уже удалён вручную
                db.mark_upload_deleted(r["id"])
            elif views == 0 and privacy == "public":
                try:
                    delete_video(yt, r["new_video_id"])
                except no_rights_exc:
                    error = ("нет права удалять ролики — перепривяжи канал (панель → проект → «Сменить»), "
                             "иначе автоудаление не работает")
                    db.mark_upload_checked(r["id"])
                    continue
                except Exception as e:  # noqa: BLE001
                    log.warning("не удалил %s: %s", r["new_video_id"], e)
                    continue                      # повторим на следующей проверке
                db.mark_upload_deleted(r["id"])
                deleted.append(r["title"] or r["new_video_id"])
            db.mark_upload_checked(r["id"])
        if deleted or error:
            report.append((project, deleted, error))
    return report
