from datetime import datetime, timezone

from monitoring.collectors.base import FetchResult
from monitoring.collectors.rss import collect

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
SOURCE = {"key": "src_media", "tier": "T3", "platform": "CROSS_PLATFORM",
          "url": "https://example.invalid/feed.xml"}

FEED = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <title>Тестовая лента</title>
  <item>
    <title>Wildberries меняет тариф хранения</title>
    <link>https://example.invalid/wb-tariff?utm_source=rss</link>
    <description>Коэффициент повышается с 3 сентября.</description>
    <pubDate>Wed, 26 Aug 2026 10:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Материал без даты</title>
    <link>https://example.invalid/no-date</link>
    <description>Тело без pubDate.</description>
  </item>
</channel></rss>"""


class FakeFetcher:
    def __init__(self, result):
        self.result = result

    def get(self, url, etag=None, last_modified=None):
        return self.result


def test_parses_entries_and_dates():
    items = collect(SOURCE, FakeFetcher(FetchResult(200, FEED)), NOW)
    assert len(items) == 2
    assert items[0].title == "Wildberries меняет тариф хранения"
    assert items[0].published_at.year == 2026
    assert items[0].published_at.month == 8


def test_strips_tracking_from_link():
    with_utm = collect(SOURCE, FakeFetcher(FetchResult(200, FEED)), NOW)
    without = collect(SOURCE, FakeFetcher(
        FetchResult(200, FEED.replace("?utm_source=rss", ""))), NOW)
    assert with_utm[0].url_hash == without[0].url_hash


def test_entry_without_date_keeps_none():
    """Источник без даты не должен получать выдуманную — это ломает свежесть."""
    items = collect(SOURCE, FakeFetcher(FetchResult(200, FEED)), NOW)
    assert items[1].published_at is None


def test_not_modified_returns_nothing():
    assert collect(SOURCE, FakeFetcher(FetchResult(304, from_cache=True)), NOW) == []


def test_error_status_returns_nothing():
    assert collect(SOURCE, FakeFetcher(FetchResult(500)), NOW) == []
