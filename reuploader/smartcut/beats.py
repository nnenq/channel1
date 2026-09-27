"""Точки реза и «биты» — смысловые куски от паузы до паузы.

Правила:
- резать можно только в паузе речи, где нет ни одного слова;
- пауза внутри фразы не годится: нужен конец фразы (знак препинания)
  или достаточно длинная пауза;
- из подходящих моментов паузы предпочитаем тот, что ближе к смене кадра.
"""
from dataclasses import dataclass, field

SENTENCE_END = (".", "!", "?", "…", "»", '"')
MIN_PHRASE_PAUSE = 0.35     # пауза без знака препинания, которую считаем концом фразы
MIN_WORD_GAP = 0.25         # минимальный промежуток между словами для реза (фоновая музыка)
SCENE_SNAP = 0.15           # смена кадра в пределах стольких секунд от паузы — режем по ней
EDGE = 0.04                 # отступ реза от краёв паузы
MIN_BEAT = 1.0              # биты короче сливаем с соседями


@dataclass
class CutPoint:
    t: float                # момент реза
    gap: tuple              # пауза (start, end), внутри которой он лежит
    scene: bool = False     # совпадает со сменой кадра
    strength: float = 0.0   # насколько «чистая» граница: длина паузы, конец предложения


@dataclass
class Beat:
    start: float
    end: float
    words: list = field(default_factory=list)
    score: float = 5.0
    tags: list = field(default_factory=list)
    why: str = ""
    must: bool = False
    refs: list = field(default_factory=list)   # индексы битов, на которые ссылается этот

    @property
    def dur(self):
        return self.end - self.start

    @property
    def text(self):
        return " ".join(w.text for w in self.words).strip()


def _pauses(analysis):
    """Промежутки без речи, в которых можно резать: (start, end, конец_фразы)."""
    words = analysis.words
    gaps = []
    if words:
        # промежутки между словами, пересечённые с тишиной или просто достаточно длинные
        for prev, nxt in zip(words, words[1:]):
            g0, g1 = prev.end, nxt.start
            if g1 - g0 < MIN_WORD_GAP:
                continue
            sentence = prev.text.rstrip().endswith(SENTENCE_END)
            if not sentence and g1 - g0 < MIN_PHRASE_PAUSE:
                continue          # короткая пауза посреди фразы — не режем
            # внутри промежутка берём участок тишины, если он есть
            quiet = [(max(s, g0), min(e, g1)) for s, e in analysis.silences if min(e, g1) - max(s, g0) > 0.08]
            if quiet:
                g0, g1 = max(quiet, key=lambda q: q[1] - q[0])
            gaps.append((g0, g1, sentence))
        # тишина до первого и после последнего слова
        first, last = words[0].start, words[-1].end
        gaps += [(s, min(e, first), True) for s, e in analysis.silences if min(e, first) - s > MIN_WORD_GAP]
        gaps += [(max(s, last), e, True) for s, e in analysis.silences if e - max(s, last) > MIN_WORD_GAP]
    else:
        gaps = [(s, e, True) for s, e in analysis.silences if e - s > MIN_WORD_GAP]
    return sorted(gaps)


def cut_points(analysis):
    points = []
    for g0, g1, sentence in _pauses(analysis):
        if g1 - g0 < 2 * EDGE:
            continue
        lo, hi = g0 + EDGE, g1 - EDGE
        near = [s for s in analysis.scenes if lo - SCENE_SNAP <= s <= hi + SCENE_SNAP]
        if near:
            t = min(max(near[0], lo), hi)
            scene = True
        else:
            t = (g0 + g1) / 2
            scene = False
        strength = min(g1 - g0, 1.5) + (0.7 if sentence else 0) + (0.5 if scene else 0)
        points.append(CutPoint(round(t, 3), (g0, g1), scene, strength))
    # точки слишком близко к началу/концу бесполезны
    return [p for p in points if 0.3 < p.t < analysis.duration - 0.3]


def make_beats(analysis, points):
    bounds = [0.0] + [p.t for p in points] + [analysis.duration]
    beats = [Beat(a, b) for a, b in zip(bounds, bounds[1:]) if b - a > 0.01]
    for w in analysis.words:
        mid = (w.start + w.end) / 2
        for b in beats:
            if b.start <= mid < b.end:
                b.words.append(w)
                break
    # сливаем слишком короткие биты с соседом (по более слабой границе)
    strength = {p.t: p.strength for p in points}
    changed = True
    while changed and len(beats) > 1:
        changed = False
        for i, b in enumerate(beats):
            if b.dur >= MIN_BEAT:
                continue
            left = strength.get(b.start, 99) if i > 0 else 99
            right = strength.get(b.end, 99) if i < len(beats) - 1 else 99
            j = i - 1 if left <= right else i + 1
            a, c = sorted((i, j))
            beats[a] = Beat(beats[a].start, beats[c].end, beats[a].words + beats[c].words)
            del beats[c]
            changed = True
            break
    return beats
