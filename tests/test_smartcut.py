import socket
import tempfile
from pathlib import Path

import pytest

from reuploader.smartcut import format_report, smart_cut
from reuploader.smartcut.media import probe
from tests.synth import frame_color, make_video

# 8 сцен ~ 64 с: у каждой своя «речь» и паузы; ключевые слова в начале и в конце
SCENES = [
    ("red", [(3.0, 0.6, "смотри что сейчас будет."), (2.5, 0.5, "это важно запомнить.")]),
    ("green", [(4.0, 0.7, "ну как бы э типа вот ну."), (3.0, 0.6, "ну э вот типа значит.")]),
    ("blue", [(4.5, 0.6, "а потом вдруг оказалось что всё иначе."), (2.0, 0.5, "вау это жесть.")]),
    ("yellow", [(5.0, 0.8, "длинная побочная история про погоду и дорогу.")]),
    ("magenta", [(4.0, 0.6, "ещё одна побочная линия без смысла."), (3.0, 0.6, "и продолжение этой линии.")]),
    ("cyan", [(4.0, 0.6, "но самое интересное впереди."), (2.5, 0.6, "смотри внимательно.")]),
    ("orange", [(4.0, 0.7, "ну э вот типа ну короче."), (3.5, 0.6, "повтор ну э вот типа.")]),
    ("purple", [(3.5, 0.6, "в итоге всё получилось."), (2.0, 0.0, "вот и всё.")]),
]


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    d = tmp_path_factory.mktemp("clip")
    src = d / "src.mp4"
    total, pauses, words, scene_starts = make_video(src, SCENES)
    return src, total, pauses, words, scene_starts


def fake_transcriber(words):
    return lambda path: list(words)


def _run(clip, target, tmp_path, **kw):
    src, total, pauses, words, _ = clip
    dst = tmp_path / "out.mp4"
    report = smart_cut(src, dst, target, transcriber=fake_transcriber(words), **kw)
    return dst, report


def test_length_within_tolerance(clip, tmp_path):
    dst, r = _run(clip, 30, tmp_path)
    assert r["status"].startswith("ok"), r
    dur = probe(dst).duration
    assert 30 * 0.95 - 0.1 <= dur <= 30 * 1.05 + 0.1, dur
    assert r["checks"]["clicks"] == 0 and r["checks"]["dips"] == 0 and r["checks"]["black_joints"] == 0
    print(format_report(r))


def test_cuts_only_in_pauses_never_in_words(clip, tmp_path):
    _, total, pauses, words, _ = clip
    _, r = _run(clip, 30, tmp_path)
    assert r["cuts"], "должен быть хотя бы один рез"
    for c in r["cuts"]:
        for t in (c["at"], c["resume"]):
            assert any(s - 0.02 <= t <= e + 0.02 for s, e in pauses), f"рез {t} не в паузе"
            assert not any(w.start < t < w.end for w in words), f"рез {t} посреди слова"


def test_hook_and_ending_kept(clip, tmp_path):
    src, total, *_ = clip
    dst, r = _run(clip, 30, tmp_path)
    out_dur = probe(dst).duration
    for t in (0.2, 1.5, 2.8):          # первые ~3 секунды — как в исходнике
        assert frame_color(dst, t) == frame_color(src, t) == "red"
    assert frame_color(dst, out_dur - 0.3) == frame_color(src, total - 0.3) == "purple"
    assert r["hook_kept"]


def test_water_removed_first(clip, tmp_path):
    _, r = _run(clip, 30, tmp_path)
    removed = " ".join(x["text"] for x in r["removed"])
    assert "ну как бы э типа" in removed or "повтор ну э вот" in removed


def test_already_short(clip, tmp_path):
    src, total, *_ = clip
    dst = tmp_path / "out.mp4"
    r = smart_cut(src, dst, total + 5, transcriber=lambda p: [])
    assert r["status"] == "already_short" and not dst.exists()


def test_free_mode_makes_no_network_requests(clip, tmp_path, monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("сетевой запрос в бесплатном режиме!")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    dst, r = _run(clip, 30, tmp_path)
    assert dst.exists() and r["status"].startswith("ok")


def test_temp_files_cleaned(clip, tmp_path):
    before = {p.name for p in Path(tempfile.gettempdir()).glob("smartcut_*")}
    _run(clip, 30, tmp_path)
    after = {p.name for p in Path(tempfile.gettempdir()).glob("smartcut_*")}
    assert after <= before


def test_background_music_no_clicks(tmp_path):
    src = tmp_path / "music.mp4"
    total, pauses, words, _ = make_video(src, SCENES, music=True)
    dst = tmp_path / "out.mp4"
    r = smart_cut(src, dst, 30, transcriber=fake_transcriber(words))
    assert r["status"].startswith("ok"), r
    assert r["checks"]["clicks"] == 0 and r["checks"]["dips"] == 0
    for c in r["cuts"]:
        assert not any(w.start < c["at"] < w.end for w in words)


def test_target_from_videos_and_parsing():
    from reuploader.smartcut.target import parse_list, target_from_videos

    vids = parse_list("0:45 1.2M\n30 900k\n1:10 50k\n20 10k\n0:58 2M\n15 5k\n40 800к\n33 700 тыс\n12 1k\n25 2k")
    assert vids[0] == (45.0, 1_200_000) and vids[7] == (33.0, 700_000)
    target, n = target_from_videos(vids)          # топ-30% из 10 = 3 ролика: 58, 45, 30
    assert n == 3 and target == 45.0
    assert target_from_videos([]) == (None, 0)
