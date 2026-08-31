"""Подтверждение сюжета несколькими источниками.

Без этого механизма фактор no_confirmation (−50) срабатывает на всём, что не
пришло из официального источника, и подъём из BACKLOG никогда не случается.
"""
from datetime import datetime, timezone

from monitoring.confirmation import count_independent_sources, repeats_of, same_story
from monitoring.models import SourceItem

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def item(title, source="src_a", platform="WILDBERRIES", h=None):
    return SourceItem(source_key=source, url=f"https://x/{h or title[:8]}",
                      url_hash=h or title[:12], title=title, body="",
                      discovered_at=NOW, published_at=NOW, platform=platform)


WB_TARIFF = "Wildberries повышает тариф хранения с третьего сентября"
WB_TARIFF_OTHER = "Тариф хранения Wildberries вырастет с сентября"
OZON_TARIFF = "Ozon повышает тариф хранения с третьего сентября"


def test_same_story_across_different_wording():
    assert same_story(item(WB_TARIFF), item(WB_TARIFF_OTHER, source="src_b"))


def test_different_platform_is_not_the_same_story():
    """Половина слов совпадает, но событие другое."""
    assert not same_story(item(WB_TARIFF),
                          item(OZON_TARIFF, source="src_b", platform="OZON"))


def test_unrelated_titles_are_not_the_same_story():
    assert not same_story(item(WB_TARIFF),
                          item("Ozon открыл склад в Казани", source="src_b"))


def test_short_titles_never_match():
    """На двух словах пересечение случайно — лучше недосчитать подтверждение."""
    assert not same_story(item("WB новости", h="a"),
                          item("WB новости", source="src_b", h="b"))


def test_single_source_counts_as_one():
    pool = [item(WB_TARIFF, h="a")]
    assert count_independent_sources(pool[0], pool) == 1


def test_three_outlets_give_three_confirmations():
    pool = [
        item(WB_TARIFF, source="src_new_retail", h="a"),
        item(WB_TARIFF_OTHER, source="src_oborot", h="b"),
        item("Хранение на Wildberries подорожает с сентября",
             source="src_habr", h="c"),
    ]
    assert count_independent_sources(pool[0], pool) == 3


def test_same_outlet_twice_is_still_one_confirmation():
    """Издание переписало свою же новость — это не независимое подтверждение."""
    pool = [
        item(WB_TARIFF, source="src_new_retail", h="a"),
        item(WB_TARIFF_OTHER, source="src_new_retail", h="b"),
    ]
    assert count_independent_sources(pool[0], pool) == 1


def test_repeats_from_the_same_outlet_are_detected():
    pool = [
        item(WB_TARIFF, source="src_new_retail", h="a"),
        item(WB_TARIFF_OTHER, source="src_new_retail", h="b"),
    ]
    assert repeats_of(pool[0], pool)


def test_confirmation_from_another_outlet_is_not_a_repeat():
    pool = [
        item(WB_TARIFF, source="src_new_retail", h="a"),
        item(WB_TARIFF_OTHER, source="src_oborot", h="b"),
    ]
    assert not repeats_of(pool[0], pool)


def test_unrelated_items_do_not_confirm():
    pool = [
        item(WB_TARIFF, source="src_a", h="a"),
        item("Wildberries открыл пункт выдачи в Твери", source="src_b", h="b"),
    ]
    assert count_independent_sources(pool[0], pool) == 1
