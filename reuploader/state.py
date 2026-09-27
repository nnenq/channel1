import json
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path("state")


class History:
    """Помнит, какие видео уже перезалиты в задаче, чтобы не было дублей."""

    def __init__(self, job_name):
        self.path = STATE_DIR / f"{job_name}.json"
        self.data = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def __contains__(self, source_id):
        return source_id in self.data

    def add(self, source_id, new_id, title):
        self.data[source_id] = {
            "new_id": new_id,
            "title": title,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
