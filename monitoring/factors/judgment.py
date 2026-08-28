"""Факторы, требующие суждения, — через Claude пачками.

Зовётся только для того, что прошло стоп-правила, и только за семью
факторами. Остальные семь считаются кодом: платить модели за разбор дат
и тиров источников незачем.

Модель не возвращает баллы и не видит порогов. Она отвечает только на вопрос
«сработал ли фактор и почему» — сумму считает scoring.py. Это не формальность:
модель, которую просят сразу назвать итоговое число, якорится на круглых
значениях, а формулу потом нельзя перекалибровать без перегенерации оценок.
"""
import json
import os

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 2048
BODY_LIMIT = 1200

JUDGMENT_KEYS = {
    "seller_money_impact", "rules_change", "has_practical_takeaway",
    "mass_effect", "legal_tax_risk", "has_conflict", "is_advertising",
}

_INSTRUCTION = """Ты размечаешь материалы для мониторинга маркетплейсов.

Для каждого материала определи, какие из факторов применимы. Ставь фактор,
только если он действительно есть, и обязательно с коротким обоснованием
по существу материала.

Факторы:
- seller_money_impact — напрямую влияет на деньги селлеров
- rules_change — меняет правила, оферту, комиссии, штрафы или логистику
- has_practical_takeaway — есть конкретное действие для продавца
- mass_effect — массовый эффект для рынка
- legal_tax_risk — судебный, налоговый или регуляторный риск
- has_conflict — конфликт, скандал или проблема
- is_advertising — рекламный материал

Ответь ТОЛЬКО объектом JSON вида:
{"<id>": {"<фактор>": "<обоснование>"}}

Материал без применимых факторов получает пустой объект.

Материалы:
"""


class AnthropicClient:
    """Обёртка над API. Ключ берётся из окружения и в код не попадает."""

    def __init__(self, model: str = MODEL):
        import anthropic
        self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._model = model

    def complete(self, prompt: str) -> str:
        message = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text


def build_prompt(items) -> str:
    blocks = [
        f"--- id: {it.url_hash}\nЗаголовок: {it.title}\nТекст: {it.body[:BODY_LIMIT]}"
        for it in items
    ]
    return _INSTRUCTION + "\n".join(blocks)


def parse_response(text: str, items) -> dict:
    """Разбирает ответ модели, отбрасывая всё, чего она не должна проставлять."""
    ids = {it.url_hash for it in items}
    out = {i: {} for i in ids}

    try:
        raw = json.loads((text or "").strip())
    except (ValueError, AttributeError):
        return out
    if not isinstance(raw, dict):
        return out

    for key, factors in raw.items():
        if key not in ids or not isinstance(factors, dict):
            continue
        out[key] = {
            name: why for name, why in factors.items()
            if name in JUDGMENT_KEYS and isinstance(why, str) and why.strip()
        }
    return out


def judgment_factors(items, client, *, batch_size: int = 15, stats=None) -> dict:
    """url_hash → {ключ_фактора: обоснование}.

    Отказ модели не роняет тик: элементы получают пустой набор факторов
    и будут доклассифицированы позже.

    Но отказ обязан быть виден. Без семи факторов-суждений материал набирает
    в лучшем случае 65 из 160, и всё уходит в DROP — снаружи это неотличимо от
    «сегодня ничего не было». Поэтому stats собирает число неудачных пачек и
    первую ошибку, а тик по ним помечает прогон деградировавшим.
    """
    result = {}
    batches = failed = 0
    first_error = None

    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        batches += 1
        try:
            text = client.complete(build_prompt(batch))
        except Exception as exc:
            failed += 1
            if first_error is None:
                first_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            result.update({it.url_hash: {} for it in batch})
            continue
        result.update(parse_response(text, batch))

    if stats is not None:
        stats["batches"] = batches
        stats["failed"] = failed
        stats["error"] = first_error

    return result
