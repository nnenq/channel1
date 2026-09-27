"""Проверка связности после отбора (бесплатный режим — по ключевым словам).

Ищем в оставшемся тексте:
- отсылки к вырезанному («как я говорил», «помнишь», «as I said»...), если прямо перед
  таким куском что-то вырезано;
- имена (слова с заглавной буквы не в начале предложения), которые впервые
  прозвучали в вырезанном куске.
Возвращает список проблем: (индекс_бита_который_нужно_вернуть, причина).
В AI-режиме связи между битами приходят от модели (beat.refs).
"""
import re

REFERENCE = re.compile(
    r"как (я|мы) (уже )?(говорил\w*|сказал\w*|видели|упоминал\w*)|помнишь|помните|выше|ранее|раньше говорил|"
    r"as i (said|mentioned)|as we saw|remember when|earlier|like i said", re.I)
NAME = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-ZА-ЯЁ][a-zа-яё]{2,})\b")


def _names(beat):
    names = set()
    for k, w in enumerate(beat.words):
        word = re.sub(r"[^\wЁё-]", "", w.text)
        prev = beat.words[k - 1].text if k else "."
        if word[:1].isupper() and len(word) > 2 and not prev.rstrip().endswith((".", "!", "?")):
            names.add(word.lower())
    return names


def find_problems(beats, kept):
    problems = []
    order = sorted(kept)
    first_seen = {}
    for i, b in enumerate(beats):
        for name in _names(b):
            first_seen.setdefault(name, i)

    for i in order:
        b = beats[i]
        # явные связи от AI
        for j in b.refs:
            if 0 <= j < len(beats) and j not in kept:
                problems.append((j, f"кусок {i + 1} ссылается на вырезанный кусок {j + 1}"))
        # «как я говорил» сразу после вырезанного
        if REFERENCE.search(b.text) and i > 0 and (i - 1) not in kept:
            problems.append((i - 1, f"в куске {i + 1} отсылка к предыдущему: «{REFERENCE.search(b.text).group(0)}»"))
        # имя, которое впервые прозвучало в вырезанном
        for name in _names(b):
            j = first_seen.get(name)
            if j is not None and j < i and j not in kept:
                problems.append((j, f"имя «{name.title()}» впервые звучит в вырезанном куске {j + 1}"))
    seen, unique = set(), []
    for j, why in problems:
        if j not in seen:
            seen.add(j)
            unique.append((j, why))
    return unique
