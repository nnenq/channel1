"""Отбор битов: «рюкзак» с сохранением порядка (динамическое программирование).

Максимизируем сумму ((оценка − средняя) × длительность) при длине в пределах цели ±допуск,
со штрафом за каждый новый кусок: выгоднее выкинуть целую побочную линию,
чем много мелких кусочков. Обязательные биты (хук, развязка) всегда остаются.
"""
import math

import numpy as np

UNIT = 0.1              # точность по длине, сек
RUN_PENALTY = 4.0       # штраф за каждый отдельный оставленный кусок (в «оценка×сек»)
BASELINE = 5.0          # «средний» бит: ниже — вода, выше — ценное
NEG = -1e18


def mark_required(beats, hook_min=3.0, hook_max=5.0, ending_min=2.0):
    """Хук — первые 3–5 секунд, развязка — последний бит(ы) не короче ending_min."""
    t = 0.0
    for b in beats:
        if t >= hook_min or (t > 0 and t + b.dur > hook_max + 2):
            break
        b.must = True
        b.tags = ["хук"] + [x for x in b.tags if x != "хук"]
        t += b.dur
    t = 0.0
    for b in reversed(beats):
        if t >= ending_min:
            break
        b.must = True
        if "развязка" not in b.tags:
            b.tags.append("развязка")
        t += b.dur
    return beats


def choose(beats, target, tolerance=0.05, forced=()):
    """Возвращает множество индексов оставляемых битов (в исходном порядке)."""
    n = len(beats)
    cap = int(math.floor(target * (1 + tolerance) / UNIT))
    low = int(math.ceil(target * (1 - tolerance) / UNIT))
    w = [max(1, int(round(b.dur / UNIT))) for b in beats]
    # Ценность относительно среднего: ценные биты «плюс», вода «минус». Отбор берёт
    # плюсовые, а до нижней границы длины добирает наименее минусовыми.
    v = [(b.score - BASELINE) * b.dur for b in beats]
    must = [b.must or i in forced for i, b in enumerate(beats)]

    best0 = np.full(cap + 1, NEG)   # последний бит НЕ взят
    best1 = np.full(cap + 1, NEG)   # последний бит взят
    best0[0] = 0.0
    keep_from1 = np.zeros((n, cap + 1), dtype=bool)   # взяли i, предыдущий тоже взят
    skip_from1 = np.zeros((n, cap + 1), dtype=bool)   # пропустили i, предыдущий был взят

    for i in range(n):
        wi = w[i]
        new1 = np.full(cap + 1, NEG)
        if wi <= cap:
            cont = best1[:cap + 1 - wi] + v[i]
            fresh = best0[:cap + 1 - wi] + v[i] - RUN_PENALTY
            keep_from1[i, wi:] = cont >= fresh
            new1[wi:] = np.maximum(cont, fresh)
        if must[i]:
            new0 = np.full(cap + 1, NEG)
        else:
            skip_from1[i] = best1 > best0
            new0 = np.maximum(best0, best1)
        best0, best1 = new0, new1

    total = np.maximum(best0, best1)
    window = total[low:cap + 1] if low <= cap else np.array([])
    if len(window) and window.max() > NEG / 2:
        c = low + int(np.argmax(window))
    else:
        # в допуск не попасть (например, обязательные куски уже длиннее цели) — ближайшее возможное
        feasible = np.where(total > NEG / 2)[0]
        if not len(feasible):
            return set(i for i in range(n) if must[i])
        c = int(feasible.max())

    state = 1 if best1[c] >= best0[c] else 0
    kept = set()
    for i in range(n - 1, -1, -1):
        if state == 1:
            kept.add(i)
            prev = 1 if keep_from1[i, c] else 0
            c -= w[i]
            state = prev
        else:
            state = 1 if skip_from1[i, c] else 0
    return kept
