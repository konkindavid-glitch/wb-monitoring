"""Проверка источника перед вводом в строй.

Источник без корректных дат ломает свежесть, источник без релевантности
засоряет поток. Не прошедший проверку получает needs_review и попадает
в дайджест — молча деградировать он не может.
"""
from dataclasses import dataclass, field
from datetime import datetime

from monitoring.collectors import rss

# Ниже этого на странице нет содержания. Порог нужен из-за одностраничных
# приложений: seller.wildberries.ru отдаёт 200 и 24 КБ разметки, а текста
# в ней 11 знаков — одно слово «Wildberries». Проверка «есть хоть какой-то
# текст» такую страницу пропускала, и источник числился рабочим, месяцами
# не давая ни одной находки.
MIN_PAGE_TEXT = 500


@dataclass(frozen=True)
class OnboardingReport:
    ok: bool
    reason: str = ""
    checks: dict = field(default_factory=dict)


def validate_source(source: dict, fetcher, now: datetime, cfg: dict) -> OnboardingReport:
    result = fetcher.get(source["url"])
    if result.status != 200:
        return OnboardingReport(False, f"источник недоступен: HTTP {result.status}")

    if source["method"] != "rss":
        # doc_diff проверяется по видимому тексту, а не по объёму разметки:
        # у SPA разметки много, а содержания нет вовсе.
        from monitoring.collectors.doc_diff import normalize_dom

        text = normalize_dom(result.text or "")
        if len(text) >= MIN_PAGE_TEXT:
            return OnboardingReport(True, "", {"text": len(text)})
        return OnboardingReport(
            False, f"страница без содержания: {len(text)} знаков текста "
                   f"при {len(result.text or '')} разметки",
            {"text": len(text)})

    items = rss.collect(source, fetcher, now)
    checks = {"items": len(items)}

    if len(items) < cfg["min_items_parsed"]:
        return OnboardingReport(
            False, f"разобрано элементов: {len(items)}", checks)

    dated = sum(1 for item in items if item.published_at is not None)
    share = dated / len(items)
    checks["dated_share"] = round(share, 2)
    if share < cfg["min_dated_share"]:
        return OnboardingReport(
            False, f"доля элементов с датами {share:.0%} ниже порога", checks)

    return OnboardingReport(True, "", checks)
