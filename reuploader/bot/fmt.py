"""Человекочитаемые числа, даты и длительности для сообщений бота."""
from datetime import datetime, timezone


def views(n):
    if n is None:
        return "?"
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def duration(sec):
    if not sec:
        return ""
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def ago(days):
    if days is None:
        return ""
    if days < 1:
        h = int(days * 24)
        return "меньше часа назад" if h < 1 else f"{h} ч назад"
    if days < 60:
        return f"{int(days)} дн. назад"
    if days < 730:
        return f"{int(days / 30)} мес. назад"
    return f"{int(days / 365)} г. назад"


def published(iso, tz):
    """'2026-09-25T15:30:00Z' -> ('25.09.2026 18:30', возраст в днях)."""
    if not iso:
        return "", None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M"), age


def fit_line(extra):
    """Строка о подгонке длины (или пусто, если не подгоняли)."""
    fit = extra.get("fit")
    if not fit:
        return ""
    if fit.get("status") == "error":
        return f"\n⚠️ Длину не подогнал: {fit.get('error', '')[:150]} — залил как есть"
    if fit.get("status") == "already_short":
        return "\n✂️ Ролик уже нужной длины — не резал"
    return f"\n✂️ Длина подогнана: {duration(fit['before'])} → {duration(fit['after'])}"


def skipped_sources_line(extra):
    bad = extra.get("skipped_sources") or {}
    if not bad:
        return ""
    names = ", ".join(u.rsplit("/", 1)[-1] for u in bad)
    return f"\n⚠️ Не прочитались источники: {names} — взял ролик из остальных"


def original_line(extra, tz):
    """Строка про оригинал: просмотры · дата выхода (сколько назад) · длительность · в день."""
    when, age = published(extra.get("published"), tz)
    parts = [f"👁 {views(extra.get('views'))}"]
    if when:
        parts.append(f"📅 {when} ({ago(age)})")
    if extra.get("duration"):
        parts.append(f"⏱ {duration(extra['duration'])}")
    if extra.get("trend_per_day"):
        parts.append(f"{'🔥 ' if extra.get('hot') else ''}+{views(extra['trend_per_day'])} за сутки")
    per_day = extra.get("views_per_day")
    if per_day is None and age is not None and extra.get("views") is not None:
        per_day = int(extra["views"] / max(age, 1))
    if per_day is not None:
        parts.append(f"≈{views(per_day)}/день в среднем")
    return " · ".join(parts)
