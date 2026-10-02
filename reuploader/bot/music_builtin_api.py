"""Файлы встроенных мелодий для прослушивания в мини-апке."""
from ..music_builtin import PRESETS, ensure


def builtin_file(settings, name):
    from .web import ApiError

    key = name.split(":", 1)[1] if ":" in name else ""
    if key not in PRESETS:
        raise ApiError("Трек не найден.", status=404)
    return ensure(settings.data_dir / "music_builtin", key)
