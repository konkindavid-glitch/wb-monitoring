"""Тесты коллектора снапшотов.

Главный тест здесь — не про срабатывание, а про молчание: счётчики просмотров
и время генерации меняются на каждом опросе, и коллектор, который на них
реагирует, за сутки приучает не читать свои сообщения.
"""
from datetime import datetime, timezone

from monitoring.collectors.base import FetchResult
from monitoring.collectors.doc_diff import (
    SnapshotStore, collect, content_hash, normalize_dom)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
SOURCE = {"key": "src_wb_tariffs", "tier": "T1", "platform": "WILDBERRIES",
          "url": "https://example.invalid/tariffs", "signal": "doc_change"}

PAGE_V1 = """<html><body>
  <nav>Меню и навигация</nav>
  <main><h1>Тарифы</h1><p>Коэффициент хранения: 0,7</p></main>
  <span class="views">Просмотров: 1024</span>
  <footer>Сформировано 27.08.2026 11:00</footer>
</body></html>"""

PAGE_NOISE = """<html><body>
  <nav>Меню и навигация</nav>
  <main><h1>Тарифы</h1><p>Коэффициент хранения: 0,7</p></main>
  <span class="views">Просмотров: 2048</span>
  <footer>Сформировано 27.08.2026 12:30</footer>
</body></html>"""

PAGE_V2 = PAGE_V1.replace("0,7", "1,1")


class FakeFetcher:
    def __init__(self, html):
        self.html = html

    def get(self, url, etag=None, last_modified=None):
        return FetchResult(200, self.html)


class MemStore(SnapshotStore):
    def __init__(self):
        self.data = {}

    def load(self, key):
        return self.data.get(key)

    def save(self, key, text):
        self.data[key] = text


def test_noise_does_not_change_the_hash():
    assert content_hash(PAGE_V1) == content_hash(PAGE_NOISE)


def test_real_change_changes_the_hash():
    assert content_hash(PAGE_V1) != content_hash(PAGE_V2)


def test_normalize_drops_chrome_and_keeps_content():
    text = normalize_dom(PAGE_V1)
    assert "Коэффициент хранения: 0,7" in text
    assert "Просмотров" not in text
    assert "Меню" not in text
    assert "Сформировано" not in text


def test_first_run_stores_snapshot_and_emits_nothing():
    store = MemStore()
    assert collect(SOURCE, FakeFetcher(PAGE_V1), store, NOW) == []
    assert store.load("src_wb_tariffs") is not None


def test_noise_on_second_run_emits_nothing():
    store = MemStore()
    collect(SOURCE, FakeFetcher(PAGE_V1), store, NOW)
    assert collect(SOURCE, FakeFetcher(PAGE_NOISE), store, NOW) == []


def test_real_change_emits_one_item_with_diff_in_body():
    store = MemStore()
    collect(SOURCE, FakeFetcher(PAGE_V1), store, NOW)
    items = collect(SOURCE, FakeFetcher(PAGE_V2), store, NOW)
    assert len(items) == 1
    assert items[0].signal == "doc_change"
    assert items[0].platform == "WILDBERRIES"
    assert "1,1" in items[0].body


def test_failed_fetch_emits_nothing_and_keeps_snapshot():
    class Broken:
        def get(self, url, etag=None, last_modified=None):
            return FetchResult(503)

    store = MemStore()
    collect(SOURCE, FakeFetcher(PAGE_V1), store, NOW)
    before = store.load("src_wb_tariffs")
    assert collect(SOURCE, Broken(), store, NOW) == []
    assert store.load("src_wb_tariffs") == before
