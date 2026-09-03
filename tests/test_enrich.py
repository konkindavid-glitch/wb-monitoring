"""Дочитывание материалов перед разметкой.

После снятия штрафа за неподтверждённость семь находок поднялись ровно
до 40 баллов — platform_wb +25 плюс is_fresh +15 и ни одного из семи
факторов-суждений. Ровное число у всех семи и выдало причину: модель
судила по анонсу из RSS, где судить не о чем.
"""
from datetime import datetime, timezone

from monitoring.collectors.base import FetchResult
from monitoring.enrich import THIN_BODY, enrich
from monitoring.models import SourceItem

NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)
ARTICLE = "<html><body><article>" + ("Подробный текст статьи. " * 60) + \
          "</article></body></html>"


def item(body, url="https://x.invalid/1"):
    return SourceItem(source_key="s", url=url, url_hash="h",
                      title="Продавцы пожаловались на задержку выплат",
                      body=body, discovered_at=NOW)


class Fetcher:
    def __init__(self, html=ARTICLE, status=200):
        self.html, self.status, self.calls = html, status, []

    def get(self, url, etag=None, last_modified=None):
        self.calls.append(url)
        return FetchResult(self.status, self.html)


def test_thin_announcement_is_read_from_the_page():
    """По анонсу «Продавцы пожаловались» нельзя сказать, меняются ли
    правила и есть ли практический вывод."""
    fetcher = Fetcher()
    out = enrich([item("Короткий анонс.")], fetcher)

    assert len(out[0].body) > THIN_BODY
    assert fetcher.calls == ["https://x.invalid/1"]


def test_full_material_is_left_alone():
    """Лишняя загрузка — лишняя задержка тика и лишний запрос к источнику."""
    fetcher = Fetcher()
    body = "Полный текст. " * 60
    out = enrich([item(body)], fetcher)

    assert out[0].body == body
    assert fetcher.calls == []


def test_unreachable_source_does_not_lose_the_find():
    """Ошибка загрузки — не повод терять находку: остаётся то, что было."""
    out = enrich([item("Короткий анонс.")], Fetcher(html="", status=403))
    assert out[0].body == "Короткий анонс."


def test_item_without_a_link_is_skipped():
    fetcher = Fetcher()
    assert enrich([item("Коротко", url="")], fetcher)[0].body == "Коротко"
    assert fetcher.calls == []


def test_budget_stops_the_reading_and_says_so():
    """Загрузчик делает до трёх попыток с нарастающим ожиданием: сотня
    мёртвых адресов иначе останавливает цикл надолго, а пока идёт тик,
    бот не слышит кнопок."""
    stats = {}
    fetcher = Fetcher()
    enrich([item("Коротко", url=f"https://x.invalid/{i}") for i in range(5)],
           fetcher, budget=0, stats=stats)

    assert fetcher.calls == []
    assert stats["skipped"] == 5
    assert stats["thin"] == 5


def test_counters_separate_read_from_thin():
    stats = {}
    enrich([item("Коротко"), item("Полный текст. " * 60)], Fetcher(),
           stats=stats)
    assert stats == {"fetched": 1, "skipped": 0, "thin": 1}


def test_shorter_page_does_not_replace_a_longer_body():
    """Страница может отдать заглушку короче анонса — тогда анонс полезнее."""
    body = "Анонс из RSS, но подлиннее обычного. " * 5
    out = enrich([item(body[:THIN_BODY - 1])], Fetcher(html="<html></html>"))
    assert out[0].body == body[:THIN_BODY - 1]
