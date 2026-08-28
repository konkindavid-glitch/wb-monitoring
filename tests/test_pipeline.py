"""Сквозной тест тракта на заглушках: без сети, без базы, без Claude."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import Deps, acquire_tick_lock, due_classes, release_tick_lock, run_tick
from monitoring.collectors.base import FetchResult
from monitoring.config import load_config

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT)
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
CADENCE = CFG.cadence_seconds()

FEED = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<item>
  <title>Wildberries меняет тариф хранения с 3 сентября</title>
  <link>https://x.invalid/wb</link>
  <description>Коэффициент хранения для категории одежда повышается с 0,7 до 1,1.
  Изменение затрагивает все поставки по схеме FBO и вступает в силу третьего
  сентября согласно обновлённой тарифной таблице площадки.</description>
  <pubDate>Thu, 27 Aug 2026 10:00:00 +0000</pubDate>
</item>
<item>
  <title>Топ-10 лучших термосов</title>
  <link>https://x.invalid/top</link>
  <description>Подборка товаров для осени, чтобы согреться в дороге и дома.
  Мы отобрали десять моделей разных ценовых категорий для любого бюджета.</description>
  <pubDate>Thu, 27 Aug 2026 10:00:00 +0000</pubDate>
</item>
</channel></rss>"""


class FakeFetcher:
    def get(self, url, etag=None, last_modified=None):
        return FetchResult(200, FEED)


class FakeRepo:
    def __init__(self):
        self.hits, self.stops, self.known = [], [], set()

    def start_run(self, cadence):
        return "run_test"

    def finish_run(self, run_id, **kw):
        self.finished = kw

    def is_known(self, url_hash):
        return url_hash in self.known

    def record_stop(self, item, verdict, run_id):
        self.stops.append((item, verdict))

    def save_hit(self, item, result, run_id):
        self.hits.append((item, result))
        self.known.add(item.url_hash)
        return f"hit_{len(self.hits):08d}"


class FakeJudge:
    """Отдаёт факторы-суждения для всех id, которые видит в промпте."""

    def complete(self, prompt):
        import json
        import re
        ids = re.findall(r"--- id: (\w+)", prompt)
        return json.dumps({
            i: {"seller_money_impact": "растёт тариф хранения",
                "rules_change": "новая тарифная таблица",
                "has_practical_takeaway": "пересчитать себестоимость",
                "mass_effect": "все поставки FBO"}
            for i in ids
        }, ensure_ascii=False)


def deps():
    return Deps(
        cfg=CFG, fetcher=FakeFetcher(), repo=FakeRepo(), judge=FakeJudge(),
        store=None,
        sources=[{"key": "src_media", "tier": "T3", "method": "rss",
                  "platform": "WILDBERRIES", "cadence": "B",
                  "url": "https://x.invalid/feed"}])


def test_roundup_is_stopped_before_scoring():
    d = deps()
    run_tick("B", d, NOW)
    assert any(v.code == "STOP_PRODUCT_ROUNDUP" for _, v in d.repo.stops)


def test_useful_item_is_scored_and_saved():
    d = deps()
    counters = run_tick("B", d, NOW)
    assert counters["scored"] == 1
    item, result = d.repo.hits[0]
    assert "Wildberries" in item.title
    assert result.score >= 60


def test_scored_item_carries_topics_and_full_breakdown():
    d = deps()
    run_tick("B", d, NOW)
    item, result = d.repo.hits[0]
    assert item.topics, "находка без тем — таксономия карты не сработала"
    assert len(result.factors) == 14


def test_second_tick_does_not_duplicate_the_same_url():
    d = deps()
    run_tick("B", d, NOW)
    run_tick("B", d, NOW)
    assert len(d.repo.hits) == 1


def test_counters_are_reported():
    counters = run_tick("B", deps(), NOW)
    assert set(counters) >= {"fetched", "stopped", "scored", "urgent", "model_calls"}
    assert counters["fetched"] == 2
    assert counters["stopped"] == 1


def test_source_of_another_cadence_class_is_not_polled():
    counters = run_tick("A", deps(), NOW)
    assert counters["fetched"] == 0


# --- расписание и блокировка ---------------------------------------------

def test_never_run_classes_are_all_due():
    assert set(due_classes({}, NOW, CADENCE)) == set(CADENCE)


def test_only_matured_classes_are_due():
    last = {k: NOW - timedelta(seconds=310) for k in CADENCE}
    assert due_classes(last, NOW, CADENCE) == ["A"]


def test_nothing_due_right_after_a_run():
    assert due_classes({k: NOW for k in CADENCE}, NOW, CADENCE) == []


def test_tick_lock_prevents_overlap():
    assert acquire_tick_lock("A") is True
    assert acquire_tick_lock("A") is False
    release_tick_lock("A")
    assert acquire_tick_lock("A") is True
    release_tick_lock("A")


def test_different_classes_do_not_block_each_other():
    assert acquire_tick_lock("A") is True
    assert acquire_tick_lock("B") is True
    release_tick_lock("A")
    release_tick_lock("B")
