from datetime import datetime, timezone
from pathlib import Path

from monitoring.config import load_config
from monitoring.models import SourceItem
from monitoring.topics import build_matchers, classify

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT)
M = build_matchers(CFG)
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def item(title, body=""):
    return SourceItem(source_key="s", url="https://x.invalid/a", url_hash="h",
                      title=title, body=body, discovered_at=NOW, published_at=NOW)


def test_all_89_queries_become_matchers():
    assert len(M) == 89


def test_commission_news_gets_money_topic_and_category():
    topics, categories = classify(
        item("Wildberries комиссии вырастут", "Комиссии по категории меняются."),
        M, CFG)
    assert "seller_money" in topics
    assert "COMMISSION_TARIFF" in categories


def test_offer_news_gets_rules_topic():
    topics, _ = classify(item("Wildberries оферта обновлена"), M, CFG)
    assert "rules_offer" in topics


def test_matching_requires_all_query_words():
    """«Wildberries» без второго слова запроса не должен давать тему комиссий."""
    topics, _ = classify(item("Wildberries открыл склад в Казани"), M, CFG)
    assert "seller_money" not in topics


def test_unmatched_item_gets_empty_tuples():
    topics, categories = classify(item("Погода в Москве на выходных"), M, CFG)
    assert topics == ()
    assert categories == ()


def test_every_returned_topic_and_category_is_declared():
    topics, categories = classify(
        item("Ozon реклама выросла, Яндекс Маркет комиссии тоже"), M, CFG)
    declared_categories = {s["category"] for t in CFG.map["topics"]
                           for s in t["subtopics"]}
    assert set(topics) <= CFG.topic_keys()
    assert set(categories) <= declared_categories


def test_item_matching_several_queries_gets_several_topics():
    topics, _ = classify(
        item("Wildberries штрафы и Wildberries реклама",
             "Меняются штрафы и рекламные ставки."), M, CFG)
    assert len(topics) >= 2
