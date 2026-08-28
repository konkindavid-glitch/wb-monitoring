"""Факторы, которые считаются кодом.

Семь из четырнадцати не требуют суждения: площадка берётся из источника,
свежесть — из дат, авторитетность — из тира, повтор — из базы. Считать их
моделью значило бы платить за то, что и так известно, и получать плавающий
результат там, где возможен точный.
"""
import re
from datetime import timedelta

FRESH_HOURS = 72
OLD_DAYS = 14
AUTHORITATIVE_TIERS = {"T1", "T2"}

_AI = re.compile(
    r"\bAI\b|\bИИ\b|нейросет|искусственн\w+\s+интеллект|GPT|LLM"
    r"|машинн\w+\s+обучени|generative", re.I)


def mechanical_factors(item, cfg=None, *, known_urls, independent_sources) -> dict:
    """Возвращает {ключ_фактора: обоснование} для скормки в score_item."""
    fired = {}

    if item.platform == "WILDBERRIES":
        fired["platform_wb"] = f"источник {item.source_key} относится к Wildberries"

    if item.tier in AUTHORITATIVE_TIERS:
        fired["authoritative_source"] = f"источник тира {item.tier}"
    elif independent_sources < 2:
        fired["no_confirmation"] = (
            f"тир {item.tier}, независимых подтверждений: {independent_sources}")

    if item.published_at is not None:
        age = item.discovered_at - item.published_at
        if age <= timedelta(hours=FRESH_HOURS):
            hours = int(age.total_seconds() // 3600)
            fired["is_fresh"] = f"возраст {hours} ч"
        elif age > timedelta(days=OLD_DAYS):
            fired["is_old"] = f"возраст {age.days} дней"

    if item.url_hash in known_urls:
        fired["is_repeat"] = "материал с таким адресом уже в базе"

    if _AI.search(f"{item.title} {item.body}"):
        fired["ai_link"] = "в тексте есть признаки темы AI"

    return fired
