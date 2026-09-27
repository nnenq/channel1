"""Цены Claude API из pricing.yaml (в коде цены не хранятся)."""
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "pricing.yaml"


def load(path=None):
    data = yaml.safe_load(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))
    if not data or "models" not in data:
        raise RuntimeError("pricing.yaml: нет раздела models")
    return data


def price(pricing, model):
    models = pricing["models"]
    if model in models:
        return models[model]
    # «claude-opus-5-20260101» и т.п. — ищем по префиксу
    for name, p in models.items():
        if model.startswith(name):
            return p
    raise KeyError(f"нет цены для модели {model} в pricing.yaml")


def cost_usd(pricing, model, input_tokens=0, output_tokens=0, cache_write=0, cache_read=0):
    p = price(pricing, model)
    return (input_tokens * p["input"] + output_tokens * p["output"]
            + cache_write * p.get("cache_write", p["input"] * 1.25)
            + cache_read * p.get("cache_read", p["input"] * 0.1)) / 1_000_000
