"""AI-оценка кусков через Claude API — только после явного согласия пользователя.

estimate(...)   — оценка токенов и цены ЛОКАЛЬНО, без сетевых запросов;
make_scorer(...) — возвращает scorer для smart_cut: один запрос в Claude со строгой
JSON-схемой ответа; фактические токены и цена пишутся в usage_log.
"""
import json
import logging

from . import pricing as pr

log = logging.getLogger("smartcut.ai")
DEFAULT_MODEL = "claude-opus-5"

SYSTEM = """Ты — редактор коротких видео. Тебе дают расшифровку ролика, разбитую на пронумерованные куски \
(с таймкодами). Ролик укоротят, выбросив часть кусков, так, чтобы зритель не заметил монтажа.

Оцени каждый кусок от 0 до 10 — насколько он нужен укороченной версии:
- 9–10: хук (цепляет в начале), поворот сюжета, шутка или пик эмоции, развязка;
- 5–8: важный для понимания сюжета кусок;
- 0–4: вода, повтор уже сказанного, отступление, которое можно выбросить целиком.
Для каждого куска укажи тег, короткое объяснение по-русски (до 12 слов) и refs — номера более ранних \
кусков, без которых этот кусок будет непонятен (например, он ссылается на них словами «как я говорил» \
или упоминает человека/предмет, который там был представлен). Если таких нет — пустой список."""

SCHEMA = {
    "type": "object",
    "properties": {
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "score": {"type": "number"},
                    "tag": {"type": "string",
                            "enum": ["hook", "twist", "joke", "emotion", "payoff", "context", "filler", "repeat"]},
                    "why": {"type": "string"},
                    "refs": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["i", "score", "tag", "why", "refs"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["beats"],
    "additionalProperties": False,
}

TAG_RU = {"hook": "хук", "twist": "поворот", "joke": "шутка", "emotion": "эмоция", "payoff": "развязка",
          "context": "контекст", "filler": "вода", "repeat": "повтор"}


def transcript(beats):
    lines = []
    for i, b in enumerate(beats):
        text = b.text or "(без речи)"
        lines.append(f"[{i}] {b.start:.1f}–{b.end:.1f} с: {text}")
    return "\n".join(lines)


def estimate(beats, model=DEFAULT_MODEL, pricing=None):
    """Оценка токенов и стоимости без сети. -> dict(input_tokens, output_tokens, usd, rub, model)."""
    pricing = pricing or pr.load()
    e = pricing.get("estimate", {})
    cpt = float(e.get("chars_per_token", 2.6))
    text = SYSTEM + json.dumps(SCHEMA, ensure_ascii=False) + transcript(beats)
    input_tokens = int(len(text) / cpt) + 200
    output_tokens = int(e.get("output_per_beat", 70)) * len(beats) + int(e.get("output_fixed", 2500))
    usd = pr.cost_usd(pricing, model, input_tokens, output_tokens)
    return {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "usd": round(usd, 4), "rub": round(usd * float(pricing.get("usd_rub", 90)), 2)}


def usage_cost(pricing, model, usage):
    """Фактическая стоимость по usage из ответа API (включая токены кэша)."""
    tokens = {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }
    return tokens, pr.cost_usd(pricing, model, **tokens)


def make_scorer(client, model=DEFAULT_MODEL, pricing=None, usage_log=None):
    """scorer(beats, analysis) для smart_cut. client — anthropic.Anthropic().

    usage_log(dict) вызывается после запроса с фактическими токенами и ценой — даже если
    ответ не удалось использовать (деньги всё равно потрачены)."""
    pricing = pricing or pr.load()

    def scorer(beats, analysis):
        # Claude Opus 5 может отклонить запрос классификатором безопасности; серверный
        # fallback переотправляет его на другую модель внутри того же вызова.
        resp = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
            system=SYSTEM,
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": transcript(beats)}],
        )
        billed_model = getattr(resp, "model", None) or model
        try:
            tokens, usd = usage_cost(pricing, billed_model, resp.usage)
        except KeyError:
            tokens, usd = usage_cost(pricing, model, resp.usage)
        if usage_log:
            usage_log({"model": billed_model, **tokens, "usd": round(usd, 6)})
        scorer.last_cost = round(usd, 4)

        if resp.stop_reason == "refusal":
            log.warning("Claude отклонил запрос — остаются эвристические оценки")
            scorer.note = "Claude отклонил запрос — использованы бесплатные эвристики"
            return beats
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)["beats"]
        except (ValueError, KeyError):
            scorer.note = "ответ Claude не разобран — использованы бесплатные эвристики"
            return beats
        for item in data:
            i = item.get("i")
            if not isinstance(i, int) or not 0 <= i < len(beats):
                continue
            b = beats[i]
            b.score = max(0.0, min(10.0, float(item["score"])))
            tag = TAG_RU.get(item["tag"], item["tag"])
            b.tags = [tag] + [t for t in b.tags if t in ("начало", "финал")]
            b.why = f"{tag}: {item['why']}".strip()
            b.refs = [r for r in item.get("refs", []) if isinstance(r, int) and 0 <= r < i]
        scorer.note = None
        return beats

    scorer.last_cost = None
    scorer.note = None
    return scorer
