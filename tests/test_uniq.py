"""Случайная уникализация и вшитые субтитры."""
import random
import shutil

import pytest

from reuploader import pipeline, source
from reuploader.effects import DEFAULT_EFFECTS, build_filter, randomize
from reuploader.smartcut.analyze import Word
from reuploader.smartcut.media import probe
from reuploader.subtitles import chunks, to_ass


def test_randomize_stays_in_safe_bounds_and_differs():
    seen = set()
    for seed in range(200):
        e = randomize(DEFAULT_EFFECTS, random.Random(seed))
        assert 1.02 <= e["zoom"] <= 1.09
        assert 0.2 <= abs(e["rotate_deg"]) <= 1.0
        assert 0.97 <= e["tempo"] <= 1.04 and not e["hflip"]          # mirror выключен по умолчанию
        assert -6 <= e["color"]["hue"] <= 6
        seen.add((e["zoom"], e["rotate_deg"], e["tempo"]))
    assert len(seen) > 150                                            # ролики действительно разные
    fixed = randomize(dict(DEFAULT_EFFECTS, random=False))
    assert fixed["zoom"] == 1.05 and "tempo" not in fixed and "color" not in fixed
    assert "setpts" not in build_filter(fixed) and "hue" not in build_filter(fixed)


def test_speed_off_and_mirror_on():
    e = randomize(dict(DEFAULT_EFFECTS, speed=False, mirror=True), random.Random(1))
    assert "tempo" not in e
    flips = sum(randomize(dict(DEFAULT_EFFECTS, mirror=True), random.Random(s))["hflip"] for s in range(100))
    assert 25 < flips < 75


def test_chunks_are_short_and_break_on_pauses():
    words = [Word(0.0, 0.3, "привет"), Word(0.3, 0.6, "как"), Word(0.6, 0.9, "дела"), Word(0.9, 1.2, "сегодня"),
             Word(2.5, 2.8, "а"), Word(2.8, 3.4, "{вот}"), Word(3.4, 3.6, "так.")]
    got = chunks(words)
    assert [t for _, _, t in got] == ["привет как дела", "сегодня", "а {вот} так."]
    assert all(e > s for s, e, _ in got)
    assert all(got[i][1] <= got[i + 1][0] for i in range(len(got) - 1))   # фразы не наезжают


def test_to_ass_escapes_and_skips_silence(tmp_path):
    assert to_ass([], 1080, 1920, tmp_path / "none.ass") is None
    p = to_ass([Word(0, 0.5, "{\\b1}жесть")], 1080, 1920, tmp_path / "s.ass")
    text = p.read_text(encoding="utf-8")
    assert "PlayResY: 1920" in text and "(B1)ЖЕСТЬ" in text and "{\\b1}" not in text


def test_animated_highlights_each_word_in_turn(tmp_path):
    words = [Word(0.0, 0.3, "раз"), Word(0.3, 0.6, "два"), Word(0.6, 0.9, "три")]
    lines = [l for l in to_ass(words, 1080, 1920, tmp_path / "a.ass").read_text(encoding="utf-8").splitlines()
             if l.startswith("Dialogue")]
    assert len(lines) == 3                                   # по строке на каждое слово
    assert "\\t(" in lines[0] and "\\t(" not in lines[1]      # «прыжок» только при появлении фразы
    for k, word in enumerate(("РАЗ", "ДВА", "ТРИ")):
        assert f"\\fscy112}}{word}" in lines[k]              # подсвечено именно текущее слово
    plain = to_ass(words, 1080, 1920, tmp_path / "p.ass", animated=False).read_text(encoding="utf-8")
    assert plain.count("Dialogue") == 1 and "\\t(" not in plain


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    from tests.synth import make_video
    from tests.test_smartcut import SCENES
    p = tmp_path_factory.mktemp("u") / "clip.mp4"
    _, _, words, _ = make_video(p, SCENES[:2])
    return p, words


def test_render_with_subtitles_and_speed(clip, tmp_path, monkeypatch):
    src, words = clip

    def dl(url, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / "v.src.mp4"
        shutil.copy(src, dst)
        return dst, {"id": "v", "title": "t", "description": "", "tags": [], "view_count": 1}
    monkeypatch.setattr(source, "download", dl)
    fx = dict(DEFAULT_EFFECTS, subtitles=True)
    _, out, meta = pipeline.prepare("u", tmp_path / "w d", fx, transcriber=lambda p: list(words))
    assert meta["subs"] == "ok" and (tmp_path / "w d" / "subs.ass").exists()
    before, after = probe(src).duration, probe(out).duration
    assert after == pytest.approx(before / meta["fx"]["tempo"], abs=0.3)   # звук и видео ускорены вместе
