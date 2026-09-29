"""Веб-сервер: мини-апка, её API и привязка YouTube-канала через Google."""
import asyncio
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from ..pipeline import rank
from . import trends
from .db import from_iso, iso, needs_youtube, utcnow
from .telegram import esc

SHORTS_ID = re.compile(r"shorts/([\w-]{11})")

WEBAPP_DIR = Path(__file__).parent / "webapp"
CHANNEL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/(@[\w.\-%]+|channel/UC[\w-]{22}|c/[\w.\-%]+|user/[\w.\-%]+)",
    re.I,
)
VIDEO_RE = re.compile(r"(?:youtube\.com/(?:shorts/|watch\?v=)|youtu\.be/)([\w-]{11})")
TIKTOK_CHANNEL_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?tiktok\.com/@([\w.\-]+)", re.I)
TIKTOK_VIDEO_RE = re.compile(r"tiktok\.com/@([\w.\-]+)/video/(\d+)", re.I)
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PRIVACY = {"public", "unlisted", "private", "scheduled"}
TOP_CACHE_TTL = 20 * 60


def normalize_video(raw):
    m = VIDEO_RE.search(str(raw))
    tt = TIKTOK_VIDEO_RE.search(str(raw))
    if m:
        return f"https://www.youtube.com/shorts/{m.group(1)}"
    if tt:
        return f"https://www.tiktok.com/@{tt.group(1)}/video/{tt.group(2)}"
    return None


def normalize_channel(url):
    """Ссылка на канал -> https://www.youtube.com/@handle (/channel/UC...) или https://www.tiktok.com/@handle."""
    url = url.strip()
    m = TIKTOK_CHANNEL_RE.match(url)
    if m:
        return "https://www.tiktok.com/@" + m.group(1)
    if url.startswith("@"):
        url = "https://www.youtube.com/" + url
    m = CHANNEL_RE.match(url)
    if not m:
        return None
    return "https://www.youtube.com/" + m.group(1)


def channel_label(url):
    if "tiktok.com/" in url:
        return "TikTok " + url.rsplit("tiktok.com/", 1)[-1]
    return url.rsplit("youtube.com/", 1)[-1]


def check_init_data(init_data, bot_token, max_age=7 * 24 * 3600):
    """Проверка подписи Telegram WebApp initData. Возвращает user dict или None."""
    if not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    if time.time() - int(pairs.get("auth_date", 0)) > max_age:
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None


class WebApp:
    def __init__(self, db, settings, scheduler, bot):
        self.db = db
        self.s = settings
        self.sched = scheduler
        self.bot = bot            # BotApp: owner_id, notify_text, public_url
        self.pending_oauth = {}   # state -> (flow, project_id, created)
        self.top_cache = {}       # channel url -> (time, videos)
        self.device_pending = {}  # project id -> {code, url, until} — привязка по коду в процессе

    # ---------- сборка приложения ----------
    def build(self):
        app = web.Application(middlewares=[self.auth_mw], client_max_size=9 * 1024 * 1024)
        r = app.router
        r.add_get("/", self.root)
        r.add_get("/app", self.index)
        r.add_get("/assets/covers.js", lambda request: web.FileResponse(WEBAPP_DIR / "covers.js",
                                                                      headers={"Cache-Control": "no-store"}))
        r.add_get("/healthz", lambda request: web.Response(text="shortsbot-ok"))
        r.add_get("/api/projects", self.list_projects)
        r.add_post("/api/projects", self.create_project)
        r.add_get("/api/projects/{pid}", self.get_project)
        r.add_patch("/api/projects/{pid}", self.patch_project)
        r.add_delete("/api/projects/{pid}", self.delete_project)
        r.add_post("/api/projects/{pid}/sources", self.add_source)
        r.add_delete("/api/projects/{pid}/sources/{sid}", self.delete_source)
        r.add_get("/api/projects/{pid}/top", self.top_videos)
        r.add_get("/api/projects/{pid}/topics", self.topics)
        r.add_post("/api/projects/{pid}/publish", self.publish)
        r.add_post("/api/projects/{pid}/replan", self.replan)
        r.add_delete("/api/projects/{pid}/slots/{sid}", self.cancel_slot)
        r.add_delete("/api/projects/{pid}/uploads/{uid}", self.delete_upload)
        r.add_post("/api/projects/{pid}/auth", self.start_oauth)
        r.add_post("/api/projects/{pid}/auth-device", self.start_device_link)
        from . import cut_api

        cut_api.setup(self, r)
        from . import cover_api
        self.covers = cover_api.setup(self, app)
        r.add_get("/api/access", self.access_list)
        r.add_post("/api/access/invite", self.access_invite)
        r.add_post("/api/access/{uid}", self.access_change)
        return app

    @staticmethod
    def from_internet(request):
        """Запрос пришёл через туннель (Cloudflare или Tailscale Funnel), а не с этого компьютера."""
        h = request.headers
        return any(k in h for k in ("Cf-Connecting-Ip", "Cf-Ray", "Tailscale-Funnel-Request",
                                    "X-Forwarded-For", "Forwarded"))

    @web.middleware
    async def auth_mw(self, request, handler):
        if request.path.startswith("/api/"):
            user = check_init_data(request.headers.get("X-Init-Data", ""), self.s.bot_token)
            if not user:
                return web.json_response({"error": "Открой панель из Telegram-бота."}, status=401)
            if not self.bot.has_access(user.get("id")):
                return web.json_response({"error": "Нет доступа — попроси владельца бота."}, status=403)
            if request.path.startswith(("/api/access", "/api/balance")) and user.get("id") != self.bot.owner_id:
                return web.json_response({"error": "Доступом управляет только владелец."}, status=403)
            request["user"] = user
        try:
            return await handler(request)
        except ApiError as e:
            return web.json_response({"error": str(e)}, status=e.status)

    # ---------- страницы ----------
    async def root(self, request):
        # Возврат из входа в Google принимается только на этом компьютере (localhost)
        if ("code" in request.query or "error" in request.query) and not self.from_internet(request):
            return await self.oauth_callback(request)
        raise web.HTTPNotFound()

    async def index(self, request):
        if not hmac.compare_digest(request.query.get("k", ""), self.bot.app_key):
            raise web.HTTPNotFound()
        return web.FileResponse(WEBAPP_DIR / "index.html", headers={
            "Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"})

    # ---------- хелперы ----------
    def _project(self, request):
        """Проект из URL — только если он принадлежит тому, кто спрашивает."""
        p = self.db.project(int(request.match_info["pid"]))
        if not p or p["user_id"] != request["user"]["id"]:
            raise ApiError("Проект не найден.", status=404)
        return p

    def _local(self, s):
        return from_iso(s).astimezone(self.s.tz)

    def _slot_json(self, slot):
        t = self._local(slot["run_at"])
        return {
            "id": slot["id"], "at": t.strftime("%Y-%m-%d %H:%M"), "time": t.strftime("%H:%M"),
            "date": t.date().isoformat(), "kind": slot["kind"], "status": slot["status"],
            "info": slot["info"], "title": slot["video_title"], "video_url": slot["video_url"],
            "retry": slot["attempt"] > 0,
        }

    def _summary(self, p):
        today = datetime.now(self.s.tz).date().isoformat()
        slots = [s for s in self.db.slots_for_date(p["id"], today) if s["status"] != "cancelled"]
        return {
            "id": p["id"], "name": p["name"], "enabled": p["enabled"],
            "channel_title": p["channel_title"], "linked": bool(p["token_path"]),
            "delivery": p["delivery"], "ready": bool(p["token_path"]) or not needs_youtube(p),
            "sources": len(self.db.sources(p["id"])),
            "today_done": sum(s["status"] == "done" for s in slots),
            "today_total": len(slots),
            "next": next((self._slot_json(s)["time"] for s in slots if s["status"] == "planned"), None),
            "exhausted": bool(p["exhausted_on"]),
        }

    # ---------- API ----------
    async def list_projects(self, request):
        now = datetime.now(self.s.tz)
        return web.json_response({
            "projects": [self._summary(p) for p in self.db.projects(request["user"]["id"])],
            "tz": str(self.s.tz), "now": now.strftime("%H:%M"),
            "is_owner": request["user"].get("id") == self.bot.owner_id,
        })

    async def create_project(self, request):
        body = await request.json()
        name = (body.get("name") or "").strip()[:60] or "Новый проект"
        pid = self.db.create_project(name, request["user"]["id"])
        return web.json_response({"id": pid})

    async def get_project(self, request):
        from ..topics import expand

        p = self._project(request)
        now = datetime.now(self.s.tz)
        since = iso(datetime.combine(now.date(), datetime.min.time(), self.s.tz))
        slots = [s for s in self.db.upcoming_slots(p["id"], since) if s["status"] != "cancelled"]
        rows = self.db.uploads(p["id"])
        uploads = [{
            "id": u["id"], "title": u["title"], "views": u["views"],
            "at": self._local(u["uploaded_at"]).strftime("%d.%m %H:%M"),
            "url": f"https://youtube.com/shorts/{u['new_video_id']}" if u["new_video_id"] else None,
            "source": f"https://youtube.com/shorts/{u['video_id']}" if len(u["video_id"]) == 11
            else (u["source_url"] or ""),
            "published": u["published"],
        } for u in rows]
        # ролики из «Плана» -> запись истории (чтобы и там можно было удалить)
        by_new_id = {u["new_video_id"]: u["id"] for u in rows if u["new_video_id"]}
        slot_json = []
        for s in slots:
            j = self._slot_json(s)
            if s["status"] == "done":
                vid = SHORTS_ID.search(s["info"] or "")
                j["upload_id"] = by_new_id.get(vid.group(1)) if vid else None
            slot_json.append(j)
        return web.json_response({
            "project": {k: p[k] for k in (
                "id", "name", "enabled", "per_day", "schedule_mode", "window_start", "window_end",
                "min_gap", "max_gap", "fixed_times", "privacy", "strategy", "effects",
                "sort_by", "max_age_days", "min_duration", "max_duration", "min_views", "fallback_old", "no_cross_dupes", "autodelete_zero", "autodelete_hours", "topic", "delivery", "cover_mode", "cover_style",
                "fit_mode", "fit_seconds", "fit_cached", "fit_min", "fit_max",
                "channel_title", "channel_id")} | {
                                                   "linked": bool(p["token_path"]),
                                                   "ready": bool(p["token_path"]) or not needs_youtube(p),
                                                   "exhausted": bool(p["exhausted_on"])},
            "sources": [{"id": s["id"], "url": s["url"], "label": channel_label(s["url"])}
                        for s in self.db.sources(p["id"])],
            "slots": slot_json,
            "uploads": uploads,
            "total_uploaded": len(self.db.uploaded_ids(p["id"])),
            "device_login": Path(self.s.device_client_secret).exists(),
            "linking": (lambda d: d if d and d["until"] > time.time() else None)(self.device_pending.get(p["id"])),
            "today": now.date().isoformat(), "now": now.strftime("%Y-%m-%dT%H:%M"),
            "tz": str(self.s.tz), "topic_terms": expand(p.get("topic") or ""),
        })

    async def patch_project(self, request):
        p = self._project(request)
        body = await request.json()
        upd = {}
        if "cover_mode" in body:
            if body["cover_mode"] not in ("off", "auto"):
                raise ApiError("Неизвестный режим обложки.")
            upd["cover_mode"] = body["cover_mode"]
        if "cover_style" in body:
            from ..covers import STYLES
            if body["cover_style"] not in STYLES:
                raise ApiError("Неизвестный стиль обложки.")
            upd["cover_style"] = body["cover_style"]
        if "name" in body:
            upd["name"] = str(body["name"]).strip()[:60] or p["name"]
        if "enabled" in body:
            upd["enabled"] = bool(body["enabled"])
        if "per_day" in body:
            upd["per_day"] = _int(body["per_day"], 1, 20, "Роликов в день")
        if "min_gap" in body or "max_gap" in body:
            mn = _int(body.get("min_gap", p["min_gap"]), 1, 600, "Минимальный промежуток")
            mx = _int(body.get("max_gap", p["max_gap"]), 1, 900, "Максимальный промежуток")
            if mx < mn:
                raise ApiError("Максимальный промежуток меньше минимального.")
            upd.update(min_gap=mn, max_gap=mx)
        for k in ("window_start", "window_end"):
            if k in body:
                if not TIME_RE.match(str(body[k])):
                    raise ApiError("Время окна в формате ЧЧ:ММ.")
                upd[k] = body[k]
        ws, we = upd.get("window_start", p["window_start"]), upd.get("window_end", p["window_end"])
        if ws >= we:
            raise ApiError("Начало окна должно быть раньше конца.")
        if "schedule_mode" in body:
            if body["schedule_mode"] not in ("auto", "fixed"):
                raise ApiError("Неизвестный режим расписания.")
            upd["schedule_mode"] = body["schedule_mode"]
        if "fixed_times" in body:
            times = sorted({str(t) for t in body["fixed_times"]})
            if not all(TIME_RE.match(t) for t in times):
                raise ApiError("Время публикаций в формате ЧЧ:ММ.")
            upd["fixed_times"] = times
        if "privacy" in body:
            if body["privacy"] not in PRIVACY:
                raise ApiError("Неизвестная приватность.")
            upd["privacy"] = body["privacy"]
        if "strategy" in body:
            if body["strategy"] not in ("rotate", "top"):
                raise ApiError("Неизвестная стратегия.")
            upd["strategy"] = body["strategy"]
        if "delivery" in body:
            if body["delivery"] not in ("youtube", "telegram", "both"):
                raise ApiError("Неизвестный способ публикации.")
            upd["delivery"] = body["delivery"]
        if "sort_by" in body:
            if body["sort_by"] not in ("trend", "views", "per_day"):
                raise ApiError("Неизвестная сортировка.")
            upd["sort_by"] = body["sort_by"]
        if "fit_mode" in body:
            if body["fit_mode"] not in ("off", "fixed", "channel", "range"):
                raise ApiError("Неизвестный режим подгонки длины.")
            upd["fit_mode"] = body["fit_mode"]
        if "fit_seconds" in body:
            from ..smartcut.target import parse_duration

            try:
                sec = parse_duration(str(body["fit_seconds"] or 0))
            except ValueError as e:
                raise ApiError(str(e)) from None
            if sec and not 5 <= sec <= 600:
                raise ApiError("Длина — от 5 секунд до 10 минут.")
            upd["fit_seconds"] = sec
        if "fit_range" in body:
            from ..smartcut.target import parse_range

            try:
                lo, hi = parse_range(str(body["fit_range"] or ""))
            except ValueError as e:
                raise ApiError(str(e)) from None
            if not 5 <= lo <= hi <= 3600:
                raise ApiError("Диапазон — от 5 секунд до 60 минут, «от» не больше «до».")
            upd.update(fit_min=lo, fit_max=hi)
        if "min_duration" in body or "max_duration" in body:
            mn = _int(body.get("min_duration", p["min_duration"]) or 0, 0, 36000, "Длина от, сек")
            mx = _int(body.get("max_duration", p["max_duration"]) or 0, 0, 36000, "Длина до, сек")
            if mx and mn > mx:
                raise ApiError("Минимальная длина больше максимальной.")
            upd.update(min_duration=mn, max_duration=mx)
        if "min_views" in body:
            from ..smartcut.target import parse_views

            try:
                upd["min_views"] = max(0, parse_views(str(body["min_views"] or "0")))
            except ValueError:
                raise ApiError("Минимум просмотров — число, например 180000 или 180K.") from None
        for k in ("fallback_old", "no_cross_dupes", "autodelete_zero"):
            if k in body:
                upd[k] = bool(body[k])
        if "autodelete_hours" in body:
            upd["autodelete_hours"] = _int(body["autodelete_hours"], 6, 168, "Через сколько часов удалять")
        if "topic" in body:
            upd["topic"] = re.sub(r"\s+", " ", str(body["topic"] or "")).strip()[:200]
        if "max_age_days" in body:
            upd["max_age_days"] = _int(body["max_age_days"] or 0, 0, 3650, "Не старше, дней")
        if "effects" in body:
            upd["effects"] = _effects(body["effects"], p["effects"])
        self._reset_exhausted(p, upd)
        self.db.update_project(p["id"], **upd)

        schedule_keys = {"enabled", "delivery", "per_day", "min_gap", "max_gap", "window_start", "window_end",
                         "schedule_mode", "fixed_times"}
        if schedule_keys & upd.keys():
            self.sched.plan_day(self.db.project(p["id"]), force=True)
            if upd.get("enabled") is False:
                self.db.cancel_future_auto(p["id"], datetime.now(self.s.tz).date().isoformat())
        return await self.get_project(request)

    async def delete_project(self, request):
        p = self._project(request)
        self.db.delete_project(p["id"])
        if p["token_path"]:
            Path(p["token_path"]).unlink(missing_ok=True)
        return web.json_response({"ok": True})

    async def add_source(self, request):
        p = self._project(request)
        body = await request.json()
        urls = [u for u in re.split(r"[\s,]+", body.get("url", "")) if u]
        if not urls:
            raise ApiError("Вставь ссылку на канал.")
        # ссылка на TikTok-видео: берём из него ID аккаунта (когда TikTok прячет его на странице профиля)
        from ..source import tiktok_id_from_video

        for k, u in enumerate(urls):
            if TIKTOK_VIDEO_RE.search(u):
                try:
                    handle, _ = await asyncio.get_running_loop().run_in_executor(None, tiktok_id_from_video, u)
                except Exception as e:  # noqa: BLE001
                    raise ApiError(f"Не смог прочитать TikTok-видео: {str(e).splitlines()[0][:200]}") from None
                urls[k] = f"https://www.tiktok.com/@{handle}"
                self.top_cache.pop(urls[k], None)
        bad = [u for u in urls if not normalize_channel(u)]
        if bad:
            raise ApiError(f"Это не ссылка на YouTube-канал или TikTok-аккаунт: {bad[0]}")
        for u in urls:
            self.db.add_source(p["id"], normalize_channel(u))
        if p["exhausted_on"]:
            self.db.update_project(p["id"], exhausted_on=None)
        return await self.get_project(request)

    def _reset_exhausted(self, p, upd):
        if p["exhausted_on"] and {"sort_by", "max_age_days", "strategy", "min_duration", "max_duration",
                                  "min_views", "fallback_old", "no_cross_dupes", "topic"} & upd.keys():
            upd["exhausted_on"] = None

    async def delete_source(self, request):
        p = self._project(request)
        self.db.delete_source(p["id"], int(request.match_info["sid"]))
        return await self.get_project(request)

    async def _source_videos(self, request, p):
        """[(подпись канала, ролики или None, ошибка)] для каналов-источников проекта (с кэшем)."""
        from ..source import enrich, list_shorts
        from ..uploader import youtube_client

        loop = asyncio.get_running_loop()
        youtube = None
        if p["token_path"]:
            try:
                youtube = await loop.run_in_executor(None, youtube_client, p["token_path"])
            except Exception:  # noqa: BLE001 — без API даты подтянутся медленнее через yt-dlp
                youtube = None

        def fetch(url):
            videos = list_shorts(url, 100)
            enrich(videos, youtube, limit=30)
            trends.record(self.db, videos, url)
            return videos

        out = []
        for s in self.db.sources(p["id"]):
            cached = self.top_cache.get(s["url"])
            if not cached or time.time() - cached[0] > TOP_CACHE_TTL or "refresh" in request.query:
                try:
                    videos = await loop.run_in_executor(None, fetch, s["url"])
                except Exception as e:  # noqa: BLE001
                    out.append((channel_label(s["url"]), None, str(e)[:200]))
                    continue
                cached = (time.time(), videos)
                self.top_cache[s["url"]] = cached
            out.append((channel_label(s["url"]), [dict(v) for v in cached[1]], None))
        return out, youtube is not None

    async def top_videos(self, request):
        """Ролики каналов-источников с датой выхода, длительностью и просмотрами в день.
        ?topic=... — только про эту тему (по умолчанию — тема проекта)."""
        from ..topics import expand

        p = self._project(request)
        topic = request.query.get("topic", p.get("topic") or "").strip()
        terms = expand(topic)
        uploaded = self.db.uploaded_ids(p["id"])
        sources, with_api = await self._source_videos(request, p)
        result = []
        for label, videos, error in sources:
            if videos is None:
                result.append({"label": label, "error": error, "videos": []})
                continue
            videos = trends.apply(self.db, videos)
            videos = rank(videos, sort_by="trend", topic_terms=terms)   # проставляет "hot"
            result.append({"label": label, "videos": [dict(v, uploaded=v["id"] in uploaded) for v in videos]})
        return web.json_response({"sources": result, "with_api": with_api, "topic": topic, "terms": terms})

    async def topics(self, request):
        """Какие известные темы (мультфильмы, игры) есть на каналах-источниках."""
        from ..topics import detect

        p = self._project(request)
        sources, _ = await self._source_videos(request, p)
        pool = [v for _, videos, _ in sources for v in (videos or [])]
        return web.json_response({"topics": [{"name": n, "count": c} for n, c in detect(pool)],
                                  "total": len(pool)})

    async def publish(self, request):
        """Залить конкретное видео: сейчас / в ближайший слот / в указанное время."""
        p = self._project(request)
        if needs_youtube(p) and not p["token_path"]:
            raise ApiError("Сначала привяжи канал для перезалива — или выбери «Мне в Telegram».")
        body = await request.json()
        if body.get("when") == "auto":
            now = datetime.now(self.s.tz)
            # «бот выберет сам»: слот без конкретного видео — выбор по правилам проекта
            if not self.db.sources(p["id"]):
                raise ApiError("Сначала добавь каналы-источники.")
            self.db.add_slot(p["id"], now.date().isoformat(), iso(now), "manual")
            self.sched.poke()
            return web.json_response({"ok": True, "at": now.strftime("%Y-%m-%d %H:%M")})
        raw = body.get("video_url", "")
        m, tt = VIDEO_RE.search(raw), TIKTOK_VIDEO_RE.search(raw)
        if m:
            url = f"https://www.youtube.com/shorts/{m.group(1)}"
        elif tt:
            url = f"https://www.tiktok.com/@{tt.group(1)}/video/{tt.group(2)}"
        else:
            raise ApiError("Нужна ссылка на видео YouTube или TikTok.")
        title = (body.get("title") or "")[:200] or None
        publication_title = str(body.get("publication_title") or "").strip()[:100] or None
        when = body.get("when", "now")
        now = datetime.now(self.s.tz)

        if when == "next":
            nxt = next((s for s in self.db.upcoming_slots(p["id"], iso(utcnow()))
                        if s["status"] == "planned" and not s["video_url"]), None)
            if not nxt:
                raise ApiError("Нет свободных запланированных слотов — выбери время вручную.")
            cover_path, cover_choice = await self.covers.selection(p["id"], url, body)
            with self.db.lock:
                changed = self.db.conn.execute(
                    "UPDATE slots SET video_url=?, video_title=?, cover_path=?, cover_choice=?, publication_title=? "
                    "WHERE id=? AND status='planned' AND video_url IS NULL",
                    (url, title, cover_path, cover_choice, publication_title, nxt["id"])).rowcount
            if not changed:
                if cover_path:
                    Path(cover_path).unlink(missing_ok=True)
                raise ApiError("Слот уже занят — выбери другой.", 409)
            self.sched.poke()
            return web.json_response({"ok": True, "at": self._slot_json(nxt)["at"]})

        if when == "now":
            at = now
        else:
            try:
                at = datetime.fromisoformat(when).replace(tzinfo=self.s.tz)
            except ValueError:
                raise ApiError("Неверная дата/время.") from None
            if at < now - timedelta(minutes=1):
                raise ApiError("Это время уже прошло.")
        cover_path, cover_choice = await self.covers.selection(p["id"], url, body)
        self.db.add_slot(p["id"], at.date().isoformat(), iso(at), "manual", url, title,
                         cover_path=cover_path, cover_choice=cover_choice, publication_title=publication_title)
        self.sched.poke()
        return web.json_response({"ok": True, "at": at.strftime("%Y-%m-%d %H:%M")})

    async def replan(self, request):
        p = self._project(request)
        if needs_youtube(p) and not p["token_path"]:
            raise ApiError("Сначала привяжи канал для перезалива — или выбери «Мне в Telegram».")
        if not p["enabled"]:
            raise ApiError("Проект на паузе.")
        self.sched.plan_day(p, force=True)
        return await self.get_project(request)

    async def cancel_slot(self, request):
        p = self._project(request)
        slot = self.db.slot(int(request.match_info["sid"]))
        if not slot or slot["project_id"] != p["id"] or slot["status"] != "planned":
            raise ApiError("Эту публикацию уже нельзя отменить.")
        self.db.set_slot(slot["id"], "cancelled", "отменено вручную")
        return await self.get_project(request)

    async def delete_upload(self, request):
        """Удалить опубликованный ролик: с YouTube и из истории (?youtube=0 — только из истории).

        Запись остаётся в базе с пометкой deleted_at: этот исходник бот больше не возьмёт."""
        from ..uploader import AuthError, NoDeleteRights, delete_video, youtube_client

        p = self._project(request)
        try:
            row = self.db.upload_row(p["id"], int(request.match_info["uid"]))
        except ValueError:
            row = None
        if not row:
            raise ApiError("Этого ролика уже нет в истории.", status=404)
        from_youtube = request.query.get("youtube", "1") != "0" and bool(row["new_video_id"])
        note = "Убрал из истории"
        if from_youtube:
            if not p["token_path"]:
                raise ApiError("Канал не привязан — удалить с YouTube не могу. Можно убрать только из истории.")

            def work():
                return delete_video(youtube_client(p["token_path"]), row["new_video_id"])
            try:
                existed = await asyncio.get_running_loop().run_in_executor(None, work)
            except NoDeleteRights:
                raise ApiError("У бота нет права удалять ролики на этом канале: канал привязан по-старому. "
                               "Нажми «Сменить» у канала и привяжи его заново — после этого удаление заработает. "
                               "Или удали ролик в YouTube Studio и убери его отсюда «только из истории».") from None
            except AuthError as e:
                raise ApiError(str(e)) from None
            except Exception as e:  # noqa: BLE001
                raise ApiError(f"YouTube не дал удалить: {type(e).__name__}: {str(e)[:200]}") from None
            note = "Удалил с YouTube и из истории" if existed else "На YouTube его уже не было — убрал из истории"
        self.db.mark_upload_deleted(row["id"])
        data = json.loads((await self.get_project(request)).text)
        data["note"] = note
        return web.json_response(data)

    # ---------- доступ (только владелец) ----------
    async def access_list(self, request):
        return web.json_response({"users": [{
            "id": u["id"], "name": u["name"], "username": u["username"], "status": u["status"],
        } for u in self.db.users()]})

    async def access_invite(self, request):
        return web.json_response({"link": self.bot.create_invite()})

    async def access_change(self, request):
        uid = int(request.match_info["uid"])
        action = (await request.json()).get("action")
        known = self.db.user(uid)
        if not known or uid == self.bot.owner_id:
            raise ApiError("Такого пользователя нет.")
        if action == "allow":
            await self.bot.grant(uid, known["name"], known["username"])
        elif action == "block":
            await self.bot.revoke(uid, block=True)
        elif action == "remove":
            await self.bot.revoke(uid)
        else:
            raise ApiError("Неизвестное действие.")
        return await self.access_list(request)

    # ---------- привязка канала через Google ----------
    async def start_oauth(self, request):
        from google_auth_oauthlib.flow import Flow

        from ..uploader import SCOPES

        p = self._project(request)
        if not Path(self.s.client_secret).exists():
            raise ApiError(f"Нет файла {self.s.client_secret} от Google Cloud — см. инструкцию.")
        flow = Flow.from_client_secrets_file(self.s.client_secret, SCOPES,
                                             redirect_uri=self.s.local_url + "/")
        url, state = flow.authorization_url(access_type="offline", prompt="consent select_account",
                                            include_granted_scopes="true")
        now = time.time()
        self.pending_oauth = {k: v for k, v in self.pending_oauth.items() if now - v[2] < 1800}
        self.pending_oauth[state] = (flow, p["id"], now)
        return web.json_response({"url": url})

    async def oauth_callback(self, request):
        state = request.query.get("state", "")
        pending = self.pending_oauth.pop(state, None)
        if "error" in request.query or not pending:
            return _page("Не получилось", "Ссылка устарела или вход отменён. Нажми «Привязать канал» ещё раз.")
        flow, pid, _ = pending
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: flow.fetch_token(code=request.query["code"]))
        except Exception as e:  # noqa: BLE001
            return _page("Ошибка", f"Google вернул ошибку: {esc(e)}")
        ok, text = await self._finish_link(pid, flow.credentials.to_json())
        return _page("Готово ✅" if ok else "Ошибка", text + (" Можно закрыть вкладку и вернуться в Telegram." if ok else ""))

    async def _finish_link(self, pid, token_text):
        """Сохраняет токен, узнаёт канал и включает проекту расписание. -> (ok, текст)."""
        from ..uploader import my_channel, youtube_client

        loop = asyncio.get_running_loop()
        self.s.tokens_dir.mkdir(parents=True, exist_ok=True)
        token_path = self.s.tokens_dir / f"project_{pid}.json"
        tmp = token_path.with_suffix(".new")
        tmp.write_text(token_text, encoding="utf-8")
        try:
            ch_id, ch_title = await loop.run_in_executor(None, lambda: my_channel(youtube_client(tmp)))
        except Exception as e:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            text = f"Google вернул ошибку: {esc(e)}"
            if "has not been used" in str(e) or "accessNotConfigured" in str(e):
                text = "В Google Cloud не включён YouTube Data API v3 — включи его и привяжи канал ещё раз."
            return False, text
        if not ch_id:
            tmp.unlink(missing_ok=True)
            return False, "У выбранного Google-аккаунта нет YouTube-канала."
        tmp.replace(token_path)
        self.db.update_project(pid, token_path=str(token_path), channel_id=ch_id, channel_title=ch_title)
        project = self.db.project(pid)
        self.sched.plan_day(project, force=True)
        await self.bot.notify_text(f"🔗 Проект <b>{esc(project['name'])}</b>: канал для перезалива — "
                                   f"<b>{esc(ch_title)}</b>", project=project)
        return True, f"Канал <b>{esc(ch_title)}</b> привязан."

    async def start_device_link(self, request):
        """Привязка по коду: возвращает код для google.com/device и ждёт ввода в фоне."""
        import aiohttp

        from . import google_device

        p = self._project(request)
        if not Path(self.s.device_client_secret).exists():
            raise ApiError(f"Нет файла {self.s.device_client_secret} — см. README, раздел про вход по коду.")
        client_id, client_secret = google_device.load_client(self.s.device_client_secret)
        async with aiohttp.ClientSession() as session:
            try:
                d = await google_device.start(session, client_id)
            except Exception as e:  # noqa: BLE001
                raise ApiError(f"Google: {e}") from None
        info = {"code": d["user_code"], "url": d.get("verification_url") or d.get("verification_uri"),
                "until": time.time() + int(d["expires_in"])}
        self.device_pending[p["id"]] = info

        async def wait():
            async with aiohttp.ClientSession() as session:
                try:
                    token = await google_device.wait_token(session, client_id, client_secret, d["device_code"],
                                                           int(d.get("interval", 5)), int(d["expires_in"]))
                    ok, text = await self._finish_link(p["id"], google_device.token_json(token, client_id, client_secret))
                except Exception as e:  # noqa: BLE001
                    ok, text = False, str(e)
            self.device_pending.pop(p["id"], None)
            if not ok:
                await self.bot.notify_text(f"❌ Проект <b>{esc(p['name'])}</b>: канал не привязан — {text}",
                                           project=p)

        asyncio.create_task(wait())
        return web.json_response(info)


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _int(v, lo, hi, name):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ApiError(f"{name}: нужно число.") from None
    if not lo <= v <= hi:
        raise ApiError(f"{name}: от {lo} до {hi}.")
    return v


def _num(v, lo, hi, name):
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ApiError(f"{name}: нужно число.") from None
    if not lo <= v <= hi:
        raise ApiError(f"{name}: от {lo} до {hi}.")
    return v


def _effects(new, old):
    e = json.loads(json.dumps(old))
    if "zoom" in new:
        e["zoom"] = _num(new["zoom"], 1.0, 1.3, "Увеличение")
    if "rotate_deg" in new:
        e["rotate_deg"] = _num(new["rotate_deg"], -5, 5, "Поворот")
    if "shadows" in new:
        e["shadows"] = _num(new["shadows"], 0, 0.5, "Тени")
    if "edge_blur" in new:
        b = new["edge_blur"] or {}
        e["edge_blur"] = {
            "height": _num(b.get("height", e["edge_blur"]["height"]), 0, 0.3, "Высота размытия"),
            "sigma": _num(b.get("sigma", e["edge_blur"]["sigma"]), 0, 60, "Сила размытия"),
        }
    for k in ("random", "speed", "mirror", "subtitles"):
        if k in new:
            e[k] = bool(new[k])
    return e


def _page(title, text):
    return web.Response(content_type="text/html", text=f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<body style="font:17px/1.5 system-ui,sans-serif;max-width:480px;margin:15vh auto;padding:0 20px;text-align:center">
<h2>{title}</h2><p>{text}</p></body>""")
