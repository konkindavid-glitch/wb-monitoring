"""Проверка источника перед вводом в строй.

Источник без корректных дат ломает свежесть, источник без релевантности
засоряет поток. Не прошедший проверку получает needs_review и попадает
в дайджест — молча деградировать он не может.
"""
from dataclasses import dataclass, field
from datetime import datetime

from monitoring.collectors import rss


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
        # doc_diff проверяется иначе: достаточно, чтобы страница отдавала текст.
        has_text = bool((result.text or "").strip())
        return OnboardingReport(has_text, "" if has_text else "пустая страница")

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
