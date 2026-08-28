"""Детерминированный отсев до подсчёта баллов.

Порядок принципиален: если считать баллы первыми, рекламный материал со
штрафом −60 может добрать сумму важностью темы. См. docs/01 §4.

Граница со штрафом no_confirmation: стоп-правило STOP_UNCONFIRMED_RUMOR
срабатывает, когда источник не назван вообще и проверять нечего. Если
источник назван, но независимых подтверждений пока нет — это фактор −50,
и материал живёт в BACKLOG до появления подтверждения. См. docs/00 §5.1.
"""
import re
from datetime import timedelta

from monitoring.models import SourceItem, StopVerdict

MAX_AGE_DAYS = 30
MIN_BODY_CHARS = 200

_SERVICE_AD = re.compile(
    r"наш сервис|попробуйте бесплатно|регистрируйтесь|промокод|реферальн"
    r"|подпишись на курс|записывайтесь", re.I)
_ROUNDUP = re.compile(r"\bтоп[- ]?\d+|подборк|лучших?\s+\d+|обзор\s+\d+", re.I)
_DISCOUNT = re.compile(r"скидк\w*\s+до\s+\d+\s*%|распродаж|чёрная пятница", re.I)
_RUMOUR = re.compile(r"говорят,|по слухам|ходят слухи|неподтверждённ|source unknown", re.I)
_MOTIVATIONAL = re.compile(
    r"верь в себя|мотивац|путь к успеху|просто начни|не сдавайся", re.I)
_MINOR_PROMO = re.compile(r"запустил\w*\s+(?:небольш\w+\s+)?акци", re.I)
_BUYER_ONLY = re.compile(r"как выбрать|что подарить|гид покупателя", re.I)

# Признаки конкретики: проценты, деньги, даты, числовые изменения.
_CONCRETE = re.compile(
    r"\d+[.,]?\d*\s*%"
    r"|\d+[.,]?\d*\s*(?:п\.?\s?п\.?|процент)"
    r"|\d+[.,]?\d*\s*(?:руб|₽)"
    r"|\bс\s+\d{1,2}\s+(?:янв|фев|мар|апр|мая|июн|июл|авг|сен|окт|ноя|дек)"
    r"|\d{2}\.\d{2}\.\d{4}"
    r"|\bс\s+\d+[.,]\d+\s+до\s+\d+[.,]\d+",
    re.I)


def check(item: SourceItem, cfg=None) -> StopVerdict:
    """Возвращает вердикт. Код всегда из config/triage.yaml → stop_rules."""
    text = f"{item.title}\n{item.body}"

    if _SERVICE_AD.search(text):
        return StopVerdict("STOP_SERVICE_AD", "признаки рекламы сервиса")
    if _ROUNDUP.search(text):
        return StopVerdict("STOP_PRODUCT_ROUNDUP", "подборка товаров")
    if _DISCOUNT.search(text):
        return StopVerdict("STOP_DISCOUNT_NOISE", "скидки без рыночного значения")
    if _MINOR_PROMO.search(text):
        return StopVerdict("STOP_MINOR_PROMO", "мелкая акция площадки")
    if _RUMOUR.search(text):
        return StopVerdict("STOP_UNCONFIRMED_RUMOR", "источник не назван")
    if _MOTIVATIONAL.search(text):
        return StopVerdict("STOP_MOTIVATIONAL", "мотивационный текст без фактов")
    if _BUYER_ONLY.search(text):
        return StopVerdict("STOP_BUYER_ONLY", "материал только для покупателей")

    if item.published_at is not None:
        age = item.discovered_at - item.published_at
        if age > timedelta(days=MAX_AGE_DAYS):
            return StopVerdict("STOP_OLD_NEWS", f"возраст {age.days} дней")

    if len(item.body) < MIN_BODY_CHARS and not _has_substance(item):
        return StopVerdict("STOP_TOO_GENERAL", f"тело {len(item.body)} символов")

    return StopVerdict()


def _has_substance(item: SourceItem) -> bool:
    """Короткий текст ещё не значит пустой.

    Отраслевые ленты часто отдают один заголовок без тела, и «В Ozon уточнили
    условия постоплаты» приходит как 40 символов. Резать такое по длине —
    терять именно то, ради чего построен мониторинг: в одном прогоне по живым
    данным это правило съело больше половины релевантных новостей лучшего
    источника.

    Поэтому длина отсеивает только материал, за которым нет ничего: ни темы из
    карты, ни конкретных чисел, ни авторитетного источника.
    """
    if item.topics:
        return True
    if item.tier in ("T1", "T2"):
        return True
    return bool(_CONCRETE.search(f"{item.title} {item.body}"))
