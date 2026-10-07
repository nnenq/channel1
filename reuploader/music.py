"""Фоновая музыка и «приятный звук» для готовых роликов.

Музыка — свои треки пользователя (папка data/music/<id>/) или встроенные мелодии. Голос и трек
сначала приводятся к одной громкости, потом музыка ставится на заданное число децибел ниже голоса
и мягко приглушается, когда говорят (sidechain) — слышно, но никогда не громче речи.
В начале — плавное появление, в конце — затухание. Итоговая громкость выравнивается
под YouTube (-14 LUFS), чтобы ролик не был тише или громче соседних в ленте.

Брать треки лучше из Фонотеки YouTube (Студия -> Фонотека): там музыка без Content ID.
Популярные песни ставить нельзя — YouTube заберёт монетизацию или заблокирует ролик.
"""
import random
from pathlib import Path

AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
# громкость музыки после выравнивания: под речью примерно на 14 / 11 / 8,5 дБ тише голоса
LEVELS = {"low": 0.35, "mid": 0.5, "high": 0.7}
LEVEL_RU = {"low": "тихо", "mid": "средне", "high": "громче"}
MAX_TRACKS = 20
MAX_MB = 15
# лёгкое улучшение картинки: чуть ярче цвета и контраст, немного резкости (до субтитров)
ENHANCE = "eq=saturation=1.08:contrast=1.03,unsharp=5:5:0.35:5:5:0"
LOUD = "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
LOOP_FADE = 0.3                                         # затухание музыки на стыке «петли»
EVEN = "loudnorm=I=-16:TP=-2,aresample=48000"          # общая громкость голоса и трека перед смешиванием


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


def audio_graph(voice, music, duration, level="mid", loop=False):
    """Звуковая часть filter_complex -> строка, результат в [a].

    voice — метка звука ролика ("0:a") или None (звука нет); music — номер входа с треком или None.
    loop — ролик-«петля»: музыка лишь чуть затухает на стыке конца и начала (а не 1,5–2 с),
    и в самом конце звук гаснет за доли секунды — без щелчка при повторе."""
    vol = LEVELS.get(level, LEVELS["mid"])
    fin, fout = (LOOP_FADE, LOOP_FADE) if loop else (1.5, 2.0)
    end = max(0.0, duration - fout)
    tail = f",afade=t=out:st={max(0.0, duration - 0.06):.3f}:d=0.06" if loop else ""
    if music is None:
        return f"[{voice}]aresample=48000,{LOUD}{tail}[a]" if voice else None
    mus = (f"[{music}:a]aresample=48000,{EVEN},volume={vol},afade=t=in:st=0:d={fin},"
           f"afade=t=out:st={end:.2f}:d={fout},atrim=0:{duration:.2f}")
    if not voice:
        return mus + f",{LOUD}{tail}[a]"
    return (f"[{voice}]aresample=48000,{EVEN},asplit=2[vo][sc];{mus}[mu];"
            # музыка чуть тише, пока звучит голос, и возвращается в паузах (мягко — не «проваливается»)
            "[mu][sc]sidechaincompress=threshold=0.1:ratio=2:attack=20:release=300[md];"
            f"[vo][md]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,{LOUD}{tail}[a]")


BUILTIN = "builtin:"


def builtin_choices():
    from .music_builtin import PRESETS

    return [{"id": BUILTIN + k, "title": v["title"]} for k, v in PRESETS.items()]


def resolve(choice, user_dir, builtin_dir, rnd=random):
    """Какой трек ставить. choice: "" — случайный из своих (если своих нет — встроенная «весёлая»),
    "builtin:<имя>" — встроенная мелодия, иначе имя своего трека. -> (путь, название) или (None, None)."""
    from .music_builtin import PRESETS, ensure

    own = tracks(user_dir)
    if choice and not choice.startswith(BUILTIN):
        p = Path(user_dir) / Path(choice).name
        if p in own:
            return p, p.name
        choice = ""                          # выбранный трек удалили — как «случайный»
    if not choice and own:
        p = pick(own, rnd)
        return p, p.name
    key = choice[len(BUILTIN):] if choice else "fun"
    if key not in PRESETS:
        key = "fun"
    return ensure(builtin_dir, key), PRESETS[key]["title"]
