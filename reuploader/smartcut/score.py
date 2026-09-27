"""Бесплатная оценка битов 0..10 по эвристикам (без сети).

Сигналы: плотность речи, всплески громкости, частота смен кадров,
ключевые слова (хук / поворот / эмоция / развязка), «вода» и повторы, позиция в ролике.
"""
import re

import numpy as np

HOOK_WORDS = r"смотри|представь|знаешь|секрет|никто|внимание|вот что|угадай|look|imagine|secret|watch|guess"
TWIST_WORDS = r"\bно\b|однако|вдруг|внезапно|оказалось|на самом деле|а потом|\bbut\b|suddenly|turns out|actually"
EMOTION_WORDS = r"вау|ого|ахах|хаха|смешно|офиг|жесть|круто|ужас|кошмар|\bwow\b|\bomg\b|haha|lol|crazy|insane"
PAYOFF_WORDS = r"в итоге|итак|короче|наконец|в конце|вот и всё|результат|finally|in the end|so that's|that's why"
FILLER_WORDS = r"\b(э+|м+|ну|как бы|типа|вот|значит|короче говоря|uh+|um+|like|you know)\b"


def _count(pattern, text):
    return len(re.findall(pattern, text, flags=re.I))


def score_heuristic(beats, analysis):
    if not beats:
        return beats
    total = analysis.duration
    median_db = float(np.median(analysis.rms_db)) if len(analysis.rms_db) else -30.0
    density = np.array([len(b.words) / b.dur if b.dur else 0 for b in beats])
    dens_ref = np.percentile(density[density > 0], 75) if (density > 0).any() else 1.0
    seen_ngrams = set()

    for i, b in enumerate(beats):
        text = b.text.lower()
        tags = []
        s = 4.0

        # речь: чем плотнее (до разумного предела), тем содержательнее
        if b.words:
            d = min(density[i] / (dens_ref or 1), 1.3)
            s += 2.0 * d
        # всплеск громкости — эмоция / пик
        loud = analysis.loudness(b.start, b.end)
        peak = float(np.percentile(loud, 95)) - median_db
        if peak > 6:
            s += min(peak / 6, 2.0)
            tags.append("пик")
        # динамика картинки
        cuts = sum(1 for t in analysis.scenes if b.start <= t < b.end)
        if cuts:
            s += min(cuts / max(b.dur, 1) * 3, 1.5)
        # ключевые слова
        for pat, tag, w in ((HOOK_WORDS, "хук", 1.5), (TWIST_WORDS, "поворот", 1.2),
                            (EMOTION_WORDS, "эмоция", 1.5), (PAYOFF_WORDS, "развязка", 1.5)):
            n = _count(pat, text)
            if n:
                s += min(n, 2) * w / 1.5
                tags.append(tag)
        # вода: слова-паразиты и повторы уже сказанного
        fillers = _count(FILLER_WORDS, text)
        if b.words and fillers / len(b.words) > 0.25:
            s -= 2.5
            tags.append("вода")
        tokens = re.findall(r"\w+", text)
        grams = {" ".join(tokens[k:k + 3]) for k in range(len(tokens) - 2)}
        if grams and len(grams & seen_ngrams) / len(grams) > 0.5:
            s -= 2.5
            tags.append("повтор")
        seen_ngrams |= grams
        # тишина без речи и без движения — скорее всего лишнее
        if not b.words and not cuts and peak <= 6:
            s -= 1.5
            tags.append("пусто")
        # позиция: начало и конец важнее
        pos = (b.start + b.end) / 2 / total
        if pos < 0.1:
            s += 1.5
            tags.append("начало")
        elif pos > 0.9:
            s += 1.5
            tags.append("финал")

        b.score = float(np.clip(s, 0, 10))
        b.tags = tags
        b.why = ", ".join(tags) if tags else "обычный фрагмент"
    return beats
