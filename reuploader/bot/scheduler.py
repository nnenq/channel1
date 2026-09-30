"""Планировщик: раз в день раскладывает публикации по времени и выполняет их."""
import asyncio
import logging
import shutil
from datetime import datetime, timedelta

from .db import from_iso, iso, needs_youtube, utcnow
from .planner import day_bounds, parse_hhmm, plan_auto
from .telegram import esc
from . import trends
from .worker import run_slot

log = logging.getLogger("scheduler")

TICK = 20                         # как часто проверять расписание, секунд
MAX_LATE = timedelta(minutes=90)  # если бот был выключен дольше — слот пропускается
RETRY_AFTER = timedelta(minutes=20)
SCHEDULE_LEAD = timedelta(minutes=15)
ZERO_CHECK_EVERY = timedelta(hours=1)    # проверка «0 просмотров»
REPLAN_MIN_DELAY = 15                # минут: после смены настроек первый ролик не раньше
PLAN_TOMORROW_HOUR = 20              # с этого часа планируем завтрашний день  # publishAt должен быть в будущем — с запасом


class Scheduler:
    def __init__(self, db, settings, notifier):
        self.db = db
        self.s = settings
        self.notify = notifier     # объект с методами uploaded/failed/exhausted/skipped
        self.wake = asyncio.Event()
        self.busy = asyncio.Lock()

    def poke(self):
        self.wake.set()

    def now_local(self):
        return datetime.now(self.s.tz).replace(microsecond=0)

    # ---------- планирование ----------
    def plan_day(self, project, day=None, force=False):
        """Создаёт автоматические слоты проекта на день (по умолчанию — сегодня).

        force=True — перепланировать: ещё не выполненные авто-слоты отменяются
        и раскладываются заново с учётом уже сделанных за день.
        """
        if not project["enabled"] or (needs_youtube(project) and not project["token_path"]):
            return []
        now = self.now_local()
        day = day or now.date()
        date_s = day.isoformat()
        if force:
            self.db.cancel_future_auto(project["id"], date_s)
        existing = [s for s in self.db.slots_for_date(project["id"], date_s)
                    if s["kind"] == "auto" and s["status"] != "cancelled" and s["attempt"] == 0]
        if existing and not force:
            return []
        earliest = now + timedelta(minutes=2)
        if force:
            # Перепланирование из-за смены настроек не должно заливать «прямо сейчас»:
            # ближайший ролик — не раньше чем через обычный промежуток (минимум 15 мин).
            earliest = now + timedelta(minutes=max(REPLAN_MIN_DELAY, project["min_gap"] or 0))

        if project["schedule_mode"] == "fixed":
            used = {from_iso(s["run_at"]).astimezone(self.s.tz).strftime("%H:%M") for s in existing}
            times = []
            for hhmm in sorted(set(project["fixed_times"])):
                try:
                    t = datetime.combine(day, parse_hhmm(hhmm), self.s.tz)
                except ValueError:
                    continue
                if t >= earliest and hhmm not in used:
                    times.append(t)
        else:
            count = project["per_day"] - len(existing)
            start, end = day_bounds(day, project["window_start"], project["window_end"], self.s.tz)
            times = plan_auto(count, max(start, earliest), end, project["min_gap"], project["max_gap"])

        for t in times:
            self.db.add_slot(project["id"], date_s, iso(t))
        if times:
            log.info("%s: запланировано %s", project["name"],
                     ", ".join(t.strftime("%H:%M") for t in times))
        return times

    def plan_all(self):
        now = self.now_local()
        for p in self.db.projects():
            self.plan_day(p)
            # Отложенные публикации: вечером сразу планируем и загружаем завтрашние,
            # чтобы днём компьютер мог быть выключен.
            if p["privacy"] == "scheduled" and p["delivery"] != "telegram" and now.hour >= PLAN_TOMORROW_HOUR:
                self.plan_day(p, now.date() + timedelta(days=1))

    # ---------- выполнение ----------
    async def run_due(self):
        async with self.busy:
            for slot in self.db.due_slots(iso(utcnow())):
                await self._run(slot)
            # Отложенные публикации загружаем сразу, как только слот появился
            for slot in self.db.early_slots(iso(utcnow() + SCHEDULE_LEAD)):
                await self._run(slot)

    async def _run(self, slot):
        project = self.db.project(slot["project_id"])
        late = utcnow() - from_iso(slot["run_at"])
        if late > MAX_LATE and slot["kind"] == "auto":
            self.db.set_slot(slot["id"], "skipped", "бот/компьютер был выключен в это время")
            await self.notify.skipped(project, slot)
            return

        self.db.set_slot(slot["id"], "running", "заливаю…")
        loop = asyncio.get_running_loop()
        from .progress import Reporter

        report = Reporter(lambda stage, f: self.db.slot_progress(slot["id"], stage, f))
        report("выбираю видео", 0.02)
        result = await loop.run_in_executor(None, run_slot, self.db, self.s, slot, report)
        self.db.set_slot(slot["id"], result.status, result.info,
                         video_title=result.title or slot.get("video_title"))
        project = self.db.project(slot["project_id"]) or project

        if result.status == "done":
            if result.extra.get("cover"):
                try:
                    await self.notify.cover(project, result)
                except Exception:
                    log.exception("Не удалось отправить обложку; видео повторно не загружаем")
            if project["exhausted_on"]:
                self.db.update_project(project["id"], exhausted_on=None)
            if result.new_id:
                await self.notify.uploaded(project, result)
            if result.extra.get("file"):
                try:
                    await self.notify.video(project, result)
                finally:
                    shutil.rmtree(result.extra["work"], ignore_errors=True)
        elif result.exhausted:
            today = self.now_local().date().isoformat()
            if project["exhausted_on"] != today:   # напоминаем не чаще раза в день
                self.db.update_project(project["id"], exhausted_on=today)
                await self.notify.exhausted(project)
        elif result.status == "failed":
            retry_at = None
            if not result.auth_problem and slot["attempt"] == 0:
                retry_at = self._retry_time(project, slot)
                if retry_at:
                    self.db.add_slot(project["id"], slot["plan_date"], iso(retry_at), slot["kind"],
                                     slot["video_url"], slot.get("video_title"), attempt=1,
                                     cover_path=slot.get("cover_path"), cover_choice=slot.get("cover_choice", "project"),
                                     publication_title=slot.get("publication_title"))
            await self.notify.failed(project, result, retry_at)

    def _retry_time(self, project, slot):
        t = utcnow() + RETRY_AFTER
        if slot["kind"] == "auto" and project["schedule_mode"] == "auto":
            _, end = day_bounds(self.now_local().date(), project["window_start"], project["window_end"], self.s.tz)
            if t > end:
                return None
        return t.astimezone(self.s.tz)

    async def snapshot(self):
        """Раз в ~2 часа замеряем просмотры роликов на каналах-источниках (для «тренда»)."""
        from .. import source
        from ..uploader import youtube_client

        if self.busy.locked():
            return
        async with self.busy:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, trends.snapshot_all, self.db, source.list_shorts,
                                       source.enrich, youtube_client)

    async def zero_views(self):
        """Удалить ролики, у которых через сутки (настройка проекта) всё ещё 0 просмотров."""
        from . import zero_views
        from ..uploader import NoDeleteRights, delete_video, youtube_client

        report = await asyncio.get_running_loop().run_in_executor(
            None, zero_views.run, self.db, youtube_client, delete_video, NoDeleteRights)
        for project, deleted, error in report:
            await self.notify.autodeleted(project, deleted, error)

    async def loop(self):
        self.db.reset_stuck()
        last_snapshot = last_zero = None
        while True:
            try:
                self.plan_all()
                await self.run_due()
                if not last_snapshot or utcnow() - last_snapshot >= timedelta(minutes=10):
                    last_snapshot = utcnow()
                    await self.snapshot()
                if not last_zero or utcnow() - last_zero >= ZERO_CHECK_EVERY:
                    last_zero = utcnow()
                    await self.zero_views()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("ошибка планировщика")
            try:
                await asyncio.wait_for(self.wake.wait(), TICK)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()


def describe_slot(slot, tz, today):
    t = from_iso(slot["run_at"]).astimezone(tz)
    day = "сегодня" if t.date() == today else ("завтра" if t.date() == today + timedelta(days=1)
                                                else t.strftime("%d.%m"))
    title = f" — {esc(slot['video_title'])}" if slot.get("video_title") else ""
    return f"{day} {t:%H:%M}{title}"
