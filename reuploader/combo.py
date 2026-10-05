"""«Всё сразу» для присланного ролика: (сократить) -> (уникализировать) -> кадр + наши субтитры + музыка.

options:
  frame — capcut | erase | strip | crop | keep (см. reuploader.resub);
  subs  — вшить наши анимированные субтитры по речи;
  uniq  — уникализация (лёгкий зум и наклон, цвет, скорость — у каждого ролика свои);
  trim  — "" (длину не менять) или «0:58» / «1:35-2:35».
Музыка — по настройкам пользователя (передаётся снаружи).
"""
import shutil
from pathlib import Path

FRAMES = {
    "capcut": "Как в CapCut",
    "erase": "Стереть субтитры",
    "strip": "Размытая полоска",
    "crop": "Обрезать полосу",
    "keep": "Кадр как есть",
}
DEFAULT = {"frame": "capcut", "subs": True, "uniq": True, "trim": ""}


def clean(opts):
    """Проверяет выбор пользователя. -> dict (ошибка — ValueError с понятным текстом)."""
    from .smartcut.target import parse_range

    o = dict(DEFAULT)
    o.update({k: v for k, v in (opts or {}).items() if k in DEFAULT})
    if o["frame"] not in FRAMES:
        raise ValueError("Неизвестный вариант кадра.")
    o["subs"], o["uniq"] = bool(o["subs"]), bool(o["uniq"])
    o["trim"] = str(o["trim"] or "").strip()
    if o["trim"]:
        parse_range(o["trim"])                       # ValueError с текстом, если формат не тот
    return o


def describe(o, music=None):
    parts = [FRAMES[o["frame"]]]
    parts.append("наши субтитры" if o["subs"] else "без наших субтитров")
    if o["uniq"]:
        parts.append("уникализация")
    if music:
        parts.append("музыка")
    if o["trim"]:
        parts.append(f"сократить до {o['trim']}")
    return " · ".join(parts)


def run(src, out, o, make_transcriber, work_dir, progress=None, music=None, music_level="mid"):
    """Делает всё по выбору. make_transcriber(путь_кэша) -> transcriber. -> отчёт (dict)."""
    from .effects import DEFAULT_EFFECTS, apply_effects, randomize
    from .resub import replace_subtitles
    from .smartcut import smart_cut
    from .smartcut.target import fit_params, parse_range

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda *_: None)
    steps = (["trim"] if o["trim"] else []) + (["uniq"] if o["uniq"] else []) + ["frame"]
    share = {"trim": 0.3, "uniq": 0.2, "frame": 0.5}
    total = sum(share[s] for s in steps)
    spans, at = {}, 0.0
    for s in steps:
        spans[s] = (at / total, (at + share[s]) / total)
        at += share[s]

    def sub(step):
        lo, hi = spans[step]
        return lambda stage, f=0.0: say(stage, lo + (hi - lo) * max(0.0, min(1.0, f)))

    report = {"kind": "combo", "options": o}
    cur = Path(src)
    if "trim" in steps:
        lo, hi = parse_range(o["trim"])
        target, tol = fit_params(lo, hi) if hi > lo else (lo, 0.05)
        cut = work / "trim.mp4"
        from .smartcut import CutError

        try:
            rep = smart_cut(cur, cut, target, tolerance=tol, transcriber=make_transcriber(work / "words_src.json"),
                            progress=sub("trim"), work_dir=work / "sc")
            report["trim"] = {"before": rep.get("before"), "after": rep.get("after"), "status": rep.get("status")}
            if cut.exists() and rep.get("status") != "already_short":
                cur = cut
        except CutError as e:            # сократить не вышло — остальное всё равно делаем
            report["trim"] = {"status": "failed", "why": str(e)}
    if "uniq" in steps:
        fx = randomize(dict(DEFAULT_EFFECTS, edge_blur=None, subtitles=False, mirror=False))
        uq = work / "uniq.mp4"
        step = sub("uniq")
        apply_effects(cur, uq, fx, progress=lambda f: step("уникализирую", f))
        report["uniq"] = {k: fx.get(k) for k in ("zoom", "rotate_deg", "tempo", "color")}
        cur = uq
    tr = make_transcriber(work / "words_final.json") if o["subs"] else (lambda p: [])
    step = sub("frame")
    rep = replace_subtitles(cur, out, tr, work / "rs", progress=lambda st, f: step(st, f), method=o["frame"],
                            music=music, music_level=music_level)
    report.update(frame=rep["mode"], words=rep["words"], music=rep.get("music"))
    shutil.rmtree(work / "sc", ignore_errors=True)
    return report
