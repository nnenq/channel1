"""Авторизация целевого канала и загрузка через YouTube Data API v3."""
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
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
        raise SystemExit(
            f"Нет токена {token_path}. Сначала: python -m reuploader auth --token {token_path}"
        )
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise SystemExit(f"Токен {token_path} невалиден — пройди auth заново.")
    return creds


def youtube_client(token_path):
    return build("youtube", "v3", credentials=_credentials(token_path))


def channel_title(youtube):
    resp = youtube.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items") or []
    return items[0]["snippet"]["title"] if items else "?"


def upload(youtube, path, title, description, tags, privacy, category_id, made_for_kids):
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
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"    загрузка {int(status.progress() * 100)}%", flush=True)
    return response["id"]
