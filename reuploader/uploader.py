"""Авторизация целевого канала и загрузка через YouTube Data API v3."""
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
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
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
