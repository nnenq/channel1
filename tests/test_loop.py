"""«Петля»: конец ролика без тишины и призывов, последний кадр перетекает в первый."""
import subprocess

import numpy as np
import pytest

from reuploader import loop, resub
from reuploader.ffmpeg_path import ffmpeg_exe
from reuploader.smartcut.analyze import Word
from reuploader.smartcut.media import probe


def _words(text, t0=0.0, step=0.5):
    return [Word(t0 + i * step, t0 + i * step + 0.4, w) for i, w in enumerate(text.split())]


def test_plan_end_drops_outro_and_silence_only():
    story = _words("Планктон уменьшил Патрика. Потом он украл формулу и сбежал в банку.")
    outro = _words("Подпишись и ставь лайк!", t0=7.0)
    end, dropped = loop.plan_end(story + outro, 10.0)
    assert dropped == "Подпишись и ставь лайк!" and end == pytest.approx(story[-1].end + loop.PAD)
    # тишина в конце убирается, а действие со звуком без слов — нет
    assert loop.plan_end(story, 10.0, quiet_from=8.0) == (8.25, "")
    assert loop.plan_end(story, 10.0) == (10.0, "")
    # «подпишись» в начале/середине ролика не трогаем; слишком много тишины — длину не меняем
    early = _words("Подпишись. Планктон уменьшил Патрика и украл формулу, а потом сбежал.")
    assert loop.plan_end(early, 8.0)[1] == ""
    assert loop.plan_end(story, 30.0, quiet_from=6.0) == (30.0, "")
    assert loop.plan_end([], 10.0, quiet_from=9.0) == (9.25, "")


def test_audio_graph_loop_has_short_fades():
    from reuploader import music
    g = music.audio_graph("0:a", 1, 20, loop=True)
    assert "afade=t=in:st=0:d=0.3" in g and "afade=t=out:st=19.70:d=0.3" in g and "st=19.940:d=0.06" in g
    assert "d=1.5" in music.audio_graph("0:a", 1, 20)


@pytest.fixture(scope="module")
def ramp(tmp_path_factory):
    """6 с: яркость кадра растёт от 0, звук 4,5 с, потом тишина."""
    p = tmp_path_factory.mktemp("loop") / "ramp.mp4"
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", "color=black:s=320x480:r=25:d=6",
                    "-f", "lavfi", "-i", "sine=f=400:d=4.5:sample_rate=44100", "-af", "apad",
                    "-vf", "geq=lum='min(250,N*2)':cb=128:cr=128", "-t", "6",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "1", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(p)], check=True)
    return p


def _luma(path):
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(path), "-vf", "scale=32:48",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, 48, 32)[:, 10:38, 4:28].mean(axis=(1, 2))


@pytest.mark.parametrize("method", ["keep", "capcut"])
def test_loop_cuts_silent_tail_and_blends_into_first_frame(ramp, tmp_path, method):
    assert loop.quiet_tail(ramp, 6.0) == pytest.approx(4.5, abs=0.15)
    heard = _words("Губка Боб открыл морозилку.", t0=0.5)
    out = tmp_path / "o.mp4"
    rep = resub.replace_subtitles(ramp, out, lambda p: heard, tmp_path / "w", method=method, loop=True)
    info = probe(out)
    assert info.duration == pytest.approx(4.75, abs=0.15) and info.audio_streams == 1
    assert rep["loop"]["cut"] == pytest.approx(1.25, abs=0.2)
    y = _luma(out)
    peak = y[: -12].max()
    assert y[-1] < 0.3 * peak                       # последний кадр почти как первый (тёмный)
    assert np.all(np.diff(y[-8:]) < 1)              # плавно, без скачка


def test_without_loop_length_is_kept(ramp, tmp_path):
    out = tmp_path / "o.mp4"
    rep = resub.replace_subtitles(ramp, out, lambda p: [], tmp_path / "w", method="keep")
    assert probe(out).duration == pytest.approx(6.0, abs=0.15) and rep["loop"] is None


def test_story_prompt_asks_for_loop():
    from reuploader.story.script import SYSTEM
    assert "Петля" in SYSTEM and "перетекать в первую" in SYSTEM
