"""Расчёт времени публикаций на день."""
import random
from datetime import datetime, time, timedelta


def parse_hhmm(s):
    h, m = s.split(":")
    return time(int(h), int(m))


def plan_auto(n, start, end, min_gap, max_gap, rng=random):
    """`n` случайных моментов в [start, end] с промежутками min_gap..max_gap минут.

    Первый ролик — в случайное время, каждый следующий — через случайный
    промежуток, но так, чтобы оставшиеся ролики ещё успели влезть в окно.
    Если окно слишком короткое, роликов будет меньше.
    """
    min_gap = timedelta(minutes=max(1, min_gap))
    max_gap = timedelta(minutes=max(min_gap.total_seconds() / 60, max_gap))
    while n > 0 and start + (n - 1) * min_gap > end:
        n -= 1
    if n <= 0 or start >= end:
        return []

    latest_first = end - (n - 1) * min_gap
    # Первый ролик скорее в первой половине допустимого интервала,
    # чтобы оставалось место на разнос остальных.
    span = (latest_first - start).total_seconds()
    first = start + timedelta(seconds=rng.uniform(0, span) * (1 if n == 1 else rng.uniform(0.3, 1)))
    times = [first]
    for i in range(1, n):
        remaining = n - i - 1
        lo = times[-1] + min_gap
        hi = min(times[-1] + max_gap, end - remaining * min_gap)
        hi = max(hi, lo)
        times.append(lo + timedelta(seconds=rng.uniform(0, (hi - lo).total_seconds())))
    return [t.replace(microsecond=0) for t in times]


def day_bounds(day, window_start, window_end, tz):
    start = datetime.combine(day, parse_hhmm(window_start), tz)
    end = datetime.combine(day, parse_hhmm(window_end), tz)
    return start, end
