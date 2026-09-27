import copy
from pathlib import Path

import yaml


def _merge(base, override):
    """Глубокое слияние словарей: override поверх base."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(path):
    path = Path(path)
    if not path.exists():
        raise SystemExit(
            f"Нет файла {path}. Сделай: cp config.example.yaml config.yaml"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults", {})
    jobs = []
    for job in raw.get("jobs", []):
        if "name" not in job or "token" not in job or not job.get("sources"):
            raise SystemExit(f"У задачи должны быть name, token и sources: {job}")
        jobs.append(_merge(defaults, job))
    return {"defaults": defaults, "jobs": jobs}
