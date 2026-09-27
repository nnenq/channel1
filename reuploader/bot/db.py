"""SQLite-хранилище: проекты, источники, расписание, история заливок."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

from ..effects import DEFAULT_EFFECTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    token_path TEXT,
    channel_id TEXT,
    channel_title TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    per_day INTEGER NOT NULL DEFAULT 2,
    schedule_mode TEXT NOT NULL DEFAULT 'auto',
    window_start TEXT NOT NULL DEFAULT '12:00',
    window_end TEXT NOT NULL DEFAULT '19:00',
    min_gap INTEGER NOT NULL DEFAULT 15,
    max_gap INTEGER NOT NULL DEFAULT 120,
    fixed_times TEXT NOT NULL DEFAULT '["13:00","16:00"]',
    privacy TEXT NOT NULL DEFAULT 'public',
    strategy TEXT NOT NULL DEFAULT 'rotate',
    sort_by TEXT NOT NULL DEFAULT 'views',
    max_age_days INTEGER NOT NULL DEFAULT 0,
    effects TEXT NOT NULL DEFAULT '{}',
    exhausted_on TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, url)
);
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_url TEXT,
    video_id TEXT NOT NULL,
    title TEXT,
    views INTEGER,
    published TEXT,
    new_video_id TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS uploads_project ON uploads(project_id, video_id);
-- Слот = одна запланированная публикация.
-- kind: auto (из расписания) | manual (назначено вручную на время / "сейчас")
-- video_url: пусто = бот сам выберет следующее видео
-- status: planned | running | done | failed | skipped | cancelled
CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    plan_date TEXT NOT NULL,
    run_at TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'auto',
    video_url TEXT,
    video_title TEXT,
    status TEXT NOT NULL DEFAULT 'planned',
    attempt INTEGER NOT NULL DEFAULT 0,
    info TEXT
);
CREATE INDEX IF NOT EXISTS slots_due ON slots(status, run_at);
"""

# Колонки, добавленные после первой версии: (таблица, колонка, определение)
MIGRATIONS = [
    ("projects", "sort_by", "TEXT NOT NULL DEFAULT 'views'"),
    ("projects", "max_age_days", "INTEGER NOT NULL DEFAULT 0"),
    ("uploads", "published", "TEXT"),
]

PROJECT_FIELDS = {
    "name", "enabled", "per_day", "schedule_mode", "window_start", "window_end",
    "min_gap", "max_gap", "fixed_times", "privacy", "strategy", "effects", "sort_by", "max_age_days",
    "token_path", "channel_id", "channel_title", "exhausted_on",
}


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(s):
    return datetime.fromisoformat(s)


class DB:
    """Одно соединение на процесс, защищённое блокировкой: запросы короткие,
    а обращаются к базе и веб-сервер, и поток заливки."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        for table, col, definition in MIGRATIONS:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
        self.lock = threading.RLock()

    def q(self, sql, *args):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, *args):
        rows = self.q(sql, *args)
        return rows[0] if rows else None

    def x(self, sql, *args):
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    # --- meta ---
    def get_meta(self, key, default=None):
        row = self.one("SELECT value FROM meta WHERE key = ?", key)
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.x("INSERT INTO meta(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, str(value))

    # --- projects ---
    def _decode(self, p):
        if p:
            p["fixed_times"] = json.loads(p["fixed_times"] or "[]")
            p["effects"] = {**DEFAULT_EFFECTS, **json.loads(p["effects"] or "{}")}
            p["enabled"] = bool(p["enabled"])
        return p

    def projects(self):
        return [self._decode(p) for p in self.q("SELECT * FROM projects ORDER BY id")]

    def project(self, pid):
        return self._decode(self.one("SELECT * FROM projects WHERE id = ?", pid))

    def create_project(self, name):
        return self.x("INSERT INTO projects(name, created_at) VALUES(?, ?)", name, iso(utcnow()))

    def update_project(self, pid, **fields):
        fields = {k: v for k, v in fields.items() if k in PROJECT_FIELDS}
        if not fields:
            return
        for k in ("fixed_times", "effects"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.x(f"UPDATE projects SET {cols} WHERE id = ?", *fields.values(), pid)

    def delete_project(self, pid):
        self.x("DELETE FROM projects WHERE id = ?", pid)

    # --- sources ---
    def sources(self, pid):
        return self.q("SELECT * FROM sources WHERE project_id = ? ORDER BY position, id", pid)

    def add_source(self, pid, url):
        pos = self.one("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM sources WHERE project_id = ?", pid)["p"]
        return self.x("INSERT OR IGNORE INTO sources(project_id, url, position) VALUES(?, ?, ?)", pid, url, pos)

    def delete_source(self, pid, sid):
        self.x("DELETE FROM sources WHERE id = ? AND project_id = ?", sid, pid)

    def sources_in_rotation_order(self, pid):
        """Каналы-источники: сначала тот, с которого дольше всего ничего не брали."""
        return [r["url"] for r in self.q(
            """SELECT s.url, MAX(u.uploaded_at) AS last
               FROM sources s LEFT JOIN uploads u
                 ON u.project_id = s.project_id AND u.source_url = s.url
               WHERE s.project_id = ?
               GROUP BY s.id
               ORDER BY last IS NOT NULL, last, s.position, s.id""", pid)]

    # --- uploads ---
    def uploaded_ids(self, pid):
        return {r["video_id"] for r in self.q("SELECT video_id FROM uploads WHERE project_id = ?", pid)}

    def add_upload(self, pid, source_url, video_id, title, views, new_id, published=None):
        self.x("""INSERT INTO uploads(project_id, source_url, video_id, title, views, published,
                                     new_video_id, uploaded_at)
                  VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
               pid, source_url, video_id, title, views, published, new_id, iso(utcnow()))

    def uploads(self, pid, limit=30):
        return self.q("SELECT * FROM uploads WHERE project_id = ? ORDER BY uploaded_at DESC LIMIT ?", pid, limit)

    def repost_candidates(self, pid, limit=3):
        """Уже перезалитые видео — самые просматриваемые, давно не повторявшиеся."""
        return self.q(
            """SELECT video_id, MAX(title) AS title, MAX(views) AS views,
                      MAX(uploaded_at) AS last, MAX(source_url) AS source_url
               FROM uploads WHERE project_id = ?
               GROUP BY video_id ORDER BY last ASC, views DESC LIMIT ?""", pid, limit)

    # --- slots ---
    def slots_for_date(self, pid, plan_date):
        return self.q("SELECT * FROM slots WHERE project_id = ? AND plan_date = ? ORDER BY run_at", pid, plan_date)

    def upcoming_slots(self, pid, since_iso):
        return self.q("""SELECT * FROM slots WHERE project_id = ?
                         AND (run_at >= ? OR status IN ('planned', 'running'))
                         ORDER BY run_at""", pid, since_iso)

    def add_slot(self, pid, plan_date, run_at_iso, kind="auto", video_url=None, video_title=None, attempt=0):
        return self.x("""INSERT INTO slots(project_id, plan_date, run_at, kind, video_url, video_title, attempt)
                         VALUES(?, ?, ?, ?, ?, ?, ?)""", pid, plan_date, run_at_iso, kind, video_url, video_title, attempt)

    def slot(self, sid):
        return self.one("SELECT * FROM slots WHERE id = ?", sid)

    def due_slots(self, now_iso):
        return self.q("""SELECT s.* FROM slots s JOIN projects p ON p.id = s.project_id
                         WHERE s.status = 'planned' AND s.run_at <= ?
                           AND (p.enabled = 1 OR s.kind = 'manual')
                         ORDER BY s.run_at""", now_iso)

    def set_slot(self, sid, status, info=None, **extra):
        cols = ["status = ?", "info = ?"] + [f"{k} = ?" for k in extra]
        self.x(f"UPDATE slots SET {', '.join(cols)} WHERE id = ?", status, info, *extra.values(), sid)

    def cancel_future_auto(self, pid, plan_date):
        self.x("""UPDATE slots SET status = 'cancelled', info = 'перепланировано'
                  WHERE project_id = ? AND plan_date = ? AND kind = 'auto' AND status = 'planned'""",
               pid, plan_date)

    def reset_stuck(self):
        self.x("UPDATE slots SET status = 'failed', info = 'бот был перезапущен во время заливки' "
               "WHERE status = 'running'")
