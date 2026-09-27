"""Пайплайн умной обрезки: анализ → биты → оценка → отбор → связность → рендер → проверка."""
import logging
import shutil
import tempfile
from pathlib import Path

import numpy as np

from . import analyze as an
from . import beats as bt
from . import coherence, render, select, verify
from .media import SR, load_audio, probe
from .score import score_heuristic

log = logging.getLogger("smartcut")
MAX_FIX_ROUNDS = 2
MAX_COHERENCE_ROUNDS = 4


class CutError(Exception):
    pass


def _runs(beats, kept):
    """Подряд идущие оставленные биты -> отрезки (start, end, [индексы])."""
    runs = []
    for i in sorted(kept):
        if runs and runs[-1][2][-1] == i - 1:
            runs[-1] = (runs[-1][0], beats[i].end, runs[-1][2] + [i])
        else:
            runs.append((beats[i].start, beats[i].end, [i]))
    return runs


def _quietest(analysis, gap, around):
    """Самая тихая точка внутри паузы (для повторного реза, если стык не прошёл проверку)."""
    g0, g1 = gap[0] + bt.EDGE, gap[1] - bt.EDGE
    if g1 <= g0:
        return around
    a, b = int(g0 / an.HOP), max(int(g0 / an.HOP) + 1, int(g1 / an.HOP))
    seg = analysis.rms_db[a:b]
    return round((a + int(np.argmin(seg))) * an.HOP + an.HOP / 2, 3) if len(seg) else around


def smart_cut(src, dst, target_sec, tolerance=0.05, transcriber=None, scorer=None,
              progress=None, work_dir=None, hook=(3.0, 5.0)):
    """Укорачивает src до target_sec (±tolerance) и пишет в dst. Возвращает отчёт (dict).

    transcriber(path) -> [Word]      — по умолчанию faster-whisper (локально);
    scorer(beats, analysis)           — внешняя оценка (AI-режим); по умолчанию эвристики.
    """
    progress = progress or (lambda stage, frac: None)
    own_tmp = work_dir is None
    work = Path(work_dir or tempfile.mkdtemp(prefix="smartcut_"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        progress("чтение файла", 0.02)
        info = probe(src)
        report = {"before": round(info.duration, 2), "target": round(target_sec, 2),
                  "tolerance": tolerance, "warnings": [], "removed": [], "cuts": []}
        if info.duration <= target_sec * (1 + tolerance):
            report.update(status="already_short", after=report["before"])
            return report

        progress("анализ звука", 0.08)
        samples = load_audio(src, work)
        analysis = an.analyze(src, info, samples, transcriber, progress)
        points = bt.cut_points(analysis)
        if not points:
            raise CutError("В ролике нет пауз, в которых можно незаметно резать.")
        beats = bt.make_beats(analysis, points)
        point_at = {p.t: p for p in points}

        progress("оценка фрагментов", 0.5)
        score_heuristic(beats, analysis)
        if scorer:
            progress("AI-оценка", 0.52)
            scorer(beats, analysis)
            if getattr(scorer, "last_cost", None) is not None:
                report["ai_cost"] = scorer.last_cost
            if getattr(scorer, "note", None):
                report["warnings"].append(scorer.note)
        select.mark_required(beats, hook_min=hook[0], hook_max=hook[1])

        progress("отбор", 0.55)
        forced = set()
        kept = select.choose(beats, target_sec, tolerance, forced)
        for _ in range(MAX_COHERENCE_ROUNDS):
            problems = [(j, why) for j, why in coherence.find_problems(beats, kept) if j not in forced]
            if not problems:
                break
            for j, why in problems:
                forced.add(j)
                report["warnings"].append("вернул кусок: " + why)
            kept = select.choose(beats, target_sec, tolerance, forced)
        runs = _runs(beats, kept)

        segments = [(s, e) for s, e, _ in runs]
        stats = {}
        for attempt in range(MAX_FIX_ROUNDS + 1):
            progress("рендер" if not attempt else f"повторный рендер ({attempt})", 0.65 + 0.1 * attempt)
            render.render(src, dst, segments, info)
            joints = list(np.cumsum([e - s for s, e in segments])[:-1])
            progress("проверка", 0.92)
            problems, stats = verify.check(dst, joints, target_sec, tolerance, work)
            joint_problems = [k for k, _ in problems if k is not None]
            if not problems or not joint_problems:
                break
            if attempt == MAX_FIX_ROUNDS:
                report["warnings"] += [why for _, why in problems]
                break
            # сдвигаем проблемные резы в самую тихую точку их паузы
            for k in set(joint_problems):
                end_t, start_t = segments[k][1], segments[k + 1][0]
                for t, idx, pos in ((end_t, k, 1), (start_t, k + 1, 0)):
                    p = point_at.get(round(t, 3))
                    if p:
                        new = _quietest(analysis, p.gap, t)
                        seg = list(segments[idx])
                        seg[pos] = new
                        segments[idx] = tuple(seg)
        for _, why in [p for p in problems if p[0] is None]:
            report["warnings"].append(why)

        report.update(
            status="ok" if not report["warnings"] else "ok_with_warnings",
            after=stats.get("duration"), checks=stats, segments=[[round(s, 2), round(e, 2)] for s, e in segments],
            cuts=[{"at": round(e, 2), "resume": round(s2, 2),
                   "scene": any(point_at.get(round(t, 3)) and point_at[round(t, 3)].scene for t in (e, s2))}
                  for (_, e), (s2, _) in zip(segments, segments[1:])],
            removed=[{"start": round(b.start, 2), "end": round(b.end, 2), "text": b.text[:300],
                      "score": round(b.score, 1), "why": b.why}
                     for i, b in enumerate(beats) if i not in kept],
            kept_beats=len(kept), total_beats=len(beats), words=len(analysis.words),
            hook_kept=all(i in kept for i, b in enumerate(beats) if "хук" in b.tags and b.must),
        )
        progress("готово", 1.0)
        return report
    finally:
        if own_tmp:
            shutil.rmtree(work, ignore_errors=True)


def prepare_beats(src, transcriber=None, work_dir=None):
    """Разметка и биты без рендера — для оценки стоимости AI-режима. -> (info, beats)."""
    own_tmp = work_dir is None
    work = Path(work_dir or tempfile.mkdtemp(prefix="smartcut_"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        info = probe(src)
        analysis = an.analyze(src, info, load_audio(src, work), transcriber)
        beats = bt.make_beats(analysis, bt.cut_points(analysis))
        score_heuristic(beats, analysis)
        return info, beats
    finally:
        if own_tmp:
            shutil.rmtree(work, ignore_errors=True)


def cached_transcriber(transcriber, cache_path):
    """Сохраняет распознанные слова в JSON: повторный прогон (после «Да» в AI-режиме) не
    запускает whisper заново."""
    import json

    def run(path):
        p = Path(cache_path)
        if p.exists():
            return [an.Word(**w) for w in json.loads(p.read_text(encoding="utf-8"))]
        words = transcriber(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps([w.__dict__ for w in words], ensure_ascii=False), encoding="utf-8")
        return words
    return run


def fmt_time(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def format_report(r):
    """Отчёт для пользователя (текст)."""
    if r["status"] == "already_short":
        return (f"Ролик уже короче цели: {fmt_time(r['before'])} ≤ {fmt_time(r['target'])} "
                f"(+{int(r['tolerance'] * 100)}%). Ничего не резал.")
    lines = [f"✂️ Было {fmt_time(r['before'])} → стало {fmt_time(r['after'])} "
             f"(цель {fmt_time(r['target'])} ±{int(r['tolerance'] * 100)}%)",
             f"Резов: {len(r['cuts'])}, все в паузах речи"
             + (f" ({sum(c['scene'] for c in r['cuts'])} совпали со сменой кадра)" if r["cuts"] else ""),
             "Хук в начале и финал сохранены." if r.get("hook_kept") else ""]
    if r.get("ai_cost") is not None:
        lines.append(f"🤖 AI-анализ: фактически ${r['ai_cost']:.4f}")
    if r["removed"]:
        lines.append("\nВырезано:")
        for x in r["removed"]:
            text = f" «{x['text'][:120]}»" if x["text"] else ""
            lines.append(f"• {fmt_time(x['start'])}–{fmt_time(x['end'])}{text} — оценка {x['score']}/10: {x['why']}")
    if r["warnings"]:
        lines.append("\n⚠️ " + "\n⚠️ ".join(r["warnings"]))
    return "\n".join(l for l in lines if l)


__all__ = ["smart_cut", "format_report", "CutError", "SR"]
