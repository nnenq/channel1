"""Авторизация целевого канала и загрузка через YouTube Data API v3."""
from datetime import timezone
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

class AuthError(Exception):
    """Нет токена или он отозван/протух — канал нужно привязать заново."""


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    # удаление роликов из панели (videos.delete) — upload/readonly для этого мало
    "https://www.googleapis.com/auth/youtube",
]


def authorize(client_secret, token_path, console=False):
    """Открывает браузер, ты выбираешь КАНАЛ, куда заливать; токен сохраняется."""
    flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
    if console:
        creds = flow.run_local_server(port=0, open_browser=False)
    else:
        creds = flow.run_local_server(port=0, prompt="consent")
    token_path = Path(token_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _credentials(token_path):
    token_path = Path(token_path)
    if not token_path.exists():
        raise AuthError(f"Нет токена {token_path} — привяжи канал (auth).")
    # Скоупы берём из самого токена: привязка по коду выдаёт общий скоуп youtube
    creds = Credentials.from_authorized_user_file(str(token_path))
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise AuthError(f"Токен {token_path} невалиден — привяжи канал заново.")
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise AuthError(f"Google отозвал доступ ({e}) — привяжи канал заново.") from e
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def youtube_client(token_path):
    return build("youtube", "v3", credentials=_credentials(token_path))


def my_channel(youtube):
    """(id, название) канала, к которому привязан токен."""
    resp = youtube.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items") or []
    if not items:
        return None, "?"
    return items[0]["id"], items[0]["snippet"]["title"]


def channel_title(youtube):
    return my_channel(youtube)[1]


def upload(youtube, path, title, description, tags, privacy, category_id, made_for_kids,
           publish_at=None, progress=None):
    """publish_at (datetime с часовым поясом) — отложенная публикация: видео загружается
    приватным, и YouTube сам делает его публичным в это время."""
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags[:30],
            "categoryId": str(category_id),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }
    if publish_at is not None:
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            if progress:
                progress(status.progress())
            else:
                print(f"    загрузка {int(status.progress() * 100)}%", flush=True)
    return response["id"]


class NoDeleteRights(Exception):
    """Токен привязан без права на удаление — нужно перепривязать канал."""


def delete_video(youtube, video_id):
    """Удаляет ролик с канала. True — удалён, False — его там уже нет."""
    from googleapiclient.errors import HttpError

    try:
        youtube.videos().delete(id=video_id).execute()
        return True
    except HttpError as e:
        if e.status_code == 404:
            return False
        if e.status_code in (401, 403):
            reasons = {d.get("reason") for d in (e.error_details or []) if isinstance(d, dict)}
            if e.status_code == 401 or reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"} \
                    or "scope" in str(e).lower():
                raise NoDeleteRights() from e
        raise


def set_thumbnail(youtube, video_id, path):
    """Separate from upload: a rejected thumbnail must never trigger a video reupload."""
    media = MediaFileUpload(str(path), mimetype="image/jpeg", resumable=False)
    return youtube.thumbnails().set(videoId=video_id, media_body=media).execute()
