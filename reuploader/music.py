"""Фоновая музыка и «приятный звук» для готовых роликов.

Музыка — свои треки пользователя (папка data/music/<id>/). Кладётся под голос тихо и
автоматически приглушается, когда говорят (sidechain), в паузах звучит чуть громче.
В начале — плавное появление, в конце — затухание. Итоговая громкость выравнивается
под YouTube (-14 LUFS), чтобы ролик не был тише или громче соседних в ленте.

Брать треки лучше из Фонотеки YouTube (Студия -> Фонотека): там музыка без Content ID.
Популярные песни ставить нельзя — YouTube заберёт монетизацию или заблокирует ролик.
"""
import random
from pathlib import Path

AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
LEVELS = {"low": 0.10, "mid": 0.17, "high": 0.26}     # громкость музыки относительно оригинала
LEVEL_RU = {"low": "тихо", "mid": "средне", "high": "громче"}
MAX_TRACKS = 20
MAX_MB = 15
# лёгкое улучшение картинки: чуть ярче цвета и контраст, немного резкости (до субтитров)
ENHANCE = "eq=saturation=1.08:contrast=1.03,unsharp=5:5:0.35:5:5:0"
LOUD = "loudnorm=I=-14:TP=-1.5:LRA=11"


def tracks(folder):
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXT and p.is_file())


def pick(paths, rnd=random):
    return rnd.choice(list(paths)) if paths else None


def start_offset(track_dur, video_dur, rnd=random):
    """С какого места трека начать: случайно, но так, чтобы хватило до конца ролика."""
    room = (track_dur or 0) - (video_dur or 0) - 1
    return round(rnd.uniform(0, min(room, 60)), 2) if room > 5 else 0.0


def music_input(path, start=0.0):
    """Аргументы ffmpeg для трека: зациклен, если короче ролика."""
    return ["-stream_loop", "-1", "-ss", f"{start:.2f}", "-i", str(Path(path).resolve())]


def audio_graph(voice, music, duration, level="mid"):
    """Звуковая часть filter_complex -> строка, результат в [a].

    voice — метка звука ролика ("0:a") или None (звука нет); music — номер входа с треком или None."""
    vol = LEVELS.get(level, LEVELS["mid"])
    end = max(0.0, duration - 2.0)
    if music is None:
        return f"[{voice}]aresample=48000,{LOUD}[a]" if voice else None
    mus = (f"[{music}:a]aresample=48000,volume={vol},afade=t=in:st=0:d=1.5,"
           f"afade=t=out:st={end:.2f}:d=2,atrim=0:{duration:.2f}")
    if not voice:
        return mus + f",{LOUD}[a]"
    return (f"[{voice}]aresample=48000,asplit=2[vo][sc];{mus}[mu];"
            # музыка тише, пока звучит голос, и возвращается в паузах
            "[mu][sc]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[md];"
            f"[vo][md]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,{LOUD}[a]")
