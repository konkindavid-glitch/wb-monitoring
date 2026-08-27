from datetime import datetime, timezone

from monitoring.normalize import clean_text, make_item, url_hash

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
SOURCE = {"key": "src_wb_news", "tier": "T1", "platform": "WILDBERRIES"}


def test_url_hash_ignores_tracking_params():
    assert url_hash("https://x.invalid/a?utm_source=tg&utm_medium=post") == \
           url_hash("https://x.invalid/a")


def test_url_hash_keeps_meaningful_params():
    assert url_hash("https://x.invalid/a?id=7") != url_hash("https://x.invalid/a")


def test_url_hash_is_stable_and_hex():
    h = url_hash("https://x.invalid/a")
    assert h == url_hash("https://x.invalid/a")
    assert len(h) == 64


def test_url_hash_ignores_trailing_slash_and_case():
    assert url_hash("https://X.Invalid/a/") == url_hash("https://x.invalid/a")


def test_clean_text_strips_markup_and_whitespace():
    assert clean_text("<p>Текст&nbsp;тут</p>\n\n\n<b>жирный</b>") == "Текст тут жирный"


def test_make_item_carries_source_metadata():
    item = make_item(SOURCE, {"url": "https://x.invalid/a", "title": "Заголовок",
                              "body": "Тело", "published_at": NOW}, NOW)
    assert item.source_key == "src_wb_news"
    assert item.tier == "T1"
    assert item.platform == "WILDBERRIES"
    assert item.discovered_at == NOW
    assert item.topics == ()
