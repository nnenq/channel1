import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass
class Settings:
    bot_token: str
    owner_id: int | None
    client_secret: str
    device_client_secret: str
    tz: ZoneInfo
    port: int
    public_url: str
    cloudflared: str
    data_dir: Path
    cut_max_mb: int = 1024
    cut_max_minutes: int = 30
    cut_link_ttl_h: int = 24
    whisper_model: str = "small"
    story_max_mb: int = 6144
    story_max_minutes: int = 180

    @property
    def db_path(self):
        return self.data_dir / "bot.db"

    @property
    def tokens_dir(self):
        return self.data_dir / "tokens"

    @property
    def work_dir(self):
        return self.data_dir / "work"

    @property
    def cut_dir(self):
        return self.data_dir / "cut"

    @property
    def story_dir(self):
        return self.data_dir / "stories"

    @property
    def local_url(self):
        return f"http://localhost:{self.port}"


def load_settings():
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("В файле .env не указан BOT_TOKEN (токен от @BotFather).")
    owner = os.getenv("OWNER_ID", "").strip()
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        bot_token=token,
        owner_id=int(owner) if owner else None,
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET", "client_secret.json"),
        device_client_secret=os.getenv("GOOGLE_DEVICE_CLIENT_SECRET", "client_secret_tv.json"),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Europe/Chisinau")),
        port=int(os.getenv("PORT", "8080")),
        public_url=os.getenv("PUBLIC_URL", "").strip().rstrip("/"),
        cloudflared=os.getenv("CLOUDFLARED", "").strip(),
        data_dir=data_dir,
        cut_max_mb=int(os.getenv("CUT_MAX_MB", "1024")),
        cut_max_minutes=int(os.getenv("CUT_MAX_MINUTES", "30")),
        cut_link_ttl_h=int(os.getenv("CUT_LINK_TTL_H", "24")),
        whisper_model=os.getenv("WHISPER_MODEL", "small"),
        story_max_mb=int(os.getenv("STORY_MAX_MB", "6144")),
        story_max_minutes=int(os.getenv("STORY_MAX_MINUTES", "180")),
    )
