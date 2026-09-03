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

# Авторитетность — только официальная лента площадки: она первоисточник,
# и подтверждать её нечем и незачем.
AUTHORITATIVE_TIERS = {"T1"}

# Штраф за отсутствие подтверждения — только для источников без редактуры:
# соцсети, форумы, телеграм-каналы, блоги. Он задуман против слухов.
#
# Раньше под него попадало всё, что не T1 и не T2, — то есть и отраслевые
# издания. На боевых данных это оказалось решающим: три работающих источника
# все T3, официальные ленты молчат, и −50 доставался КАЖДОЙ находке. Типичная
# новость набирала 80 положительных, падала до 30 и уходила в DROP. Из 176
# находок в базе 175 оказались отброшены, а пересчёт со здоровым
# классификатором не поднял ни одной: дело было не в разметке.
#
# Публикация в отраслевом издании с редактурой — не слух. Ей не полагается
# ни премии за авторитетность, ни штрафа за неподтверждённость: она просто
# считается по существу.
UNVERIFIED_TIERS = {"T4", "T5", "T6"}

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
    elif item.tier in UNVERIFIED_TIERS and independent_sources < 2:
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
