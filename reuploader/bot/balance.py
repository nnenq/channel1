"""Примерный остаток на счёте Claude API: «пополнено − потрачено».

У API нет эндпоинта баланса. Потраченное считаем сами по usage каждого запроса бота;
если в .env есть ANTHROPIC_ADMIN_KEY — раз в час берём расходы организации из
Usage & Cost Admin API (/v1/organizations/cost_report): они точнее и включают траты вне бота.
Ключи нигде не показываются и не пишутся в логи.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import aiohttp

from .db import from_iso, iso, utcnow

log = logging.getLogger("balance")
COST_URL = "https://api.anthropic.com/v1/organizations/cost_report"
REFRESH_EVERY = timedelta(hours=1)


def ai_enabled():
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def low_threshold():
    try:
        return float(os.getenv("CLAUDE_LOW_BALANCE_USD", "5"))
    except ValueError:
        return 5.0


class Balance:
    def __init__(self, db):
        self.db = db

    def first_topup_date(self):
        t = self.db.topups()
        return t[0]["date"] if t else None

    def summary(self):
        topups = self.db.topups()
        topped = round(sum(t["amount"] for t in topups), 2)
        since = self.first_topup_date()
        since_iso = f"{since}T00:00:00+00:00" if since else None
        spent, source, updated = self.db.ai_spent(since_iso), "бот", None
        admin = self.db.get_meta("admin_spent")
        if admin is not None and self.db.get_meta("admin_since") == (since or ""):
            spent, source, updated = float(admin), "Anthropic Console", self.db.get_meta("admin_spent_at")
        return {
            "topped": topped, "spent": round(spent, 4), "remaining": round(topped - spent, 2) if topups else None,
            "source": source, "updated_at": updated or iso(utcnow()), "topups": topups,
            "admin_key": bool(os.getenv("ANTHROPIC_ADMIN_KEY", "").strip()), "threshold": low_threshold(),
        }

    def can_afford(self, usd):
        s = self.summary()
        return s["remaining"] is None or s["remaining"] >= usd, s

    async def refresh_admin(self, force=False):
        """Расходы организации из cost_report (раз в час). Ошибки — только в лог, без ключа."""
        key = os.getenv("ANTHROPIC_ADMIN_KEY", "").strip()
        since = self.first_topup_date()
        if not key or not since:
            return
        last = self.db.get_meta("admin_spent_at")
        if not force and last and self.db.get_meta("admin_since") == since \
                and utcnow() - from_iso(last) < REFRESH_EVERY:
            return
        start = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        params = {"starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": "31"}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        total_cents, pages = 0.0, 0
        try:
            async with aiohttp.ClientSession() as session:
                while pages < 30:
                    async with session.get(COST_URL, params=params, headers=headers,
                                           timeout=aiohttp.ClientTimeout(total=30)) as r:
                        if r.status != 200:
                            log.warning("cost_report: HTTP %s", r.status)
                            return
                        data = await r.json()
                    for bucket in data.get("data", []):
                        for res in bucket.get("results", []):
                            total_cents += float(res.get("amount") or 0)   # decimal-строка в центах
                    pages += 1
                    if not data.get("has_more") or not data.get("next_page"):
                        break
                    params["page"] = data["next_page"]
        except Exception as e:  # noqa: BLE001
            log.warning("cost_report недоступен: %s", type(e).__name__)
            return
        self.db.set_meta("admin_spent", round(total_cents / 100, 4))
        self.db.set_meta("admin_since", since)
        self.db.set_meta("admin_spent_at", iso(utcnow()))

    async def check_low(self, notify):
        """Предупреждение админу, когда остаток опустился ниже порога (один раз до пополнения)."""
        s = self.summary()
        if s["remaining"] is None:
            return
        low = s["remaining"] < s["threshold"]
        warned = self.db.get_meta("low_balance_warned") == "1"
        if low and not warned:
            self.db.set_meta("low_balance_warned", "1")
            await notify(f"⚠️ Баланс Claude API почти закончился: остаток ≈ ${s['remaining']:.2f} "
                         f"(порог ${s['threshold']:.2f}). Пополни на console.anthropic.com → Billing "
                         f"и внеси пополнение в панели.")
        elif not low and warned:
            self.db.set_meta("low_balance_warned", "0")
