"""Проценты выполнения: ffmpeg, помощник Reporter и этапы подготовки ролика."""
import shutil

from reuploader import ffprog, pipeline, source
from reuploader.bot.progress import Reporter
from reuploader.effects import DEFAULT_EFFECTS, apply_effects
from reuploader.ffmpeg_path import ffmpeg_exe
from tests.synth import make_video
from tests.test_smartcut import SCENES


def test_reporter_throttles_and_never_goes_back():
    saved, now = [], [0.0]
    r = Reporter(lambda st, f: saved.append((st, f)), lo=0.1, hi=0.9, clock=lambda: now[0])
    r("a", 0.0); r("a", 0.5); now[0] = 2; r("a", 0.5); r("b", 0.3)      # смена этапа — пишем сразу
    now[0] = 5; r("b", 1.0)
    assert saved == [("a", 0.1), ("a", 0.5), ("b", 0.5), ("b", 0.9)]
    sub = Reporter(lambda st, f: saved.append((st, f)), clock=lambda: 0).sub(0.8, 1.0)
    sub("загрузка", 0.5)
    assert saved[-1] == ("загрузка", 0.9)


def test_ffmpeg_progress_is_real(tmp_path):
    src = tmp_path / "in.mp4"
    make_video(src, SCENES[:2], size="160x120")
    seen = []
    apply_effects(src, tmp_path / "out.mp4", dict(DEFAULT_EFFECTS, random=False), progress=seen.append)
    assert seen and seen[-1] == 1.0 and seen == sorted(seen) and any(0 < x < 1 for x in seen)
    try:                                                     # ошибка ffmpeg не теряется
        ffprog.run([ffmpeg_exe(), "-i", str(tmp_path / "nope.mp4"), str(tmp_path / "x.mp4")], 10, seen.append)
        raise AssertionError("должна быть ошибка")
    except Exception as e:  # noqa: BLE001
        assert "nope.mp4" in str(getattr(e, "stderr", "")) or "returned non-zero" in str(e)


def test_prepare_reports_stages(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    make_video(clip, SCENES[:2], size="160x120")

    def dl(url, out_dir, progress=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        if progress:
            progress(0.5); progress(1.0)
        shutil.copy(clip, out_dir / "v.src.mp4")
        return out_dir / "v.src.mp4", {"id": "v", "title": "t", "description": "", "tags": [], "view_count": 1}
    monkeypatch.setattr(source, "download", dl)
    stages = []
    pipeline.prepare("u", tmp_path / "w", dict(DEFAULT_EFFECTS, random=False), progress=lambda st, f: stages.append((st, f)))
    names = [s for s, _ in stages]
    assert names[0] == "скачиваю видео" and "уникализирую видео" in names
    assert stages[-1][1] == 1.0 and all(0 <= f <= 1 for _, f in stages)
