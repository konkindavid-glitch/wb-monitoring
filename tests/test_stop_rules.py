from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from monitoring.config import load_config
from monitoring.models import SourceItem
from monitoring.stop_rules import check

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT)
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

USEFUL_BODY = (
    "Коэффициент хранения для категории одежда повышается с 0,7 до 1,1. "
    "Изменение затрагивает все поставки по схеме FBO и вступает в силу "
    "3 сентября 2026 года согласно обновлённой тарифной таблице площадки.")


def item(title="Заголовок", body=USEFUL_BODY, published=None,
         url="https://example.invalid/a", tier="T3"):
    return SourceItem(
        source_key="src_test", url=url, url_hash="h", title=title, body=body,
        discovered_at=NOW, published_at=published or NOW, tier=tier)


@pytest.mark.parametrize("title,expected", [
    ("Топ-10 лучших термосов на осень", "STOP_PRODUCT_ROUNDUP"),
    ("Скидки до 70% на распродаже", "STOP_DISCOUNT_NOISE"),
    ("Наш сервис аналитики — попробуйте бесплатно", "STOP_SERVICE_AD"),
    ("Говорят, WB поднимет комиссию", "STOP_UNCONFIRMED_RUMOR"),
    ("Верь в себя и всё получится", "STOP_MOTIVATIONAL"),
    ("Ozon запустил акцию для покупателей", "STOP_MINOR_PROMO"),
    ("Как выбрать термос на зиму", "STOP_BUYER_ONLY"),
])
def test_rules_fire_with_expected_code(title, expected):
    assert check(item(title=title), CFG).code == expected


def test_old_material_is_stopped():
    assert check(item(published=NOW - timedelta(days=45)), CFG).code == "STOP_OLD_NEWS"


def test_short_body_is_too_general():
    assert check(item(body="Коротко."), CFG).code == "STOP_TOO_GENERAL"


def test_useful_material_passes():
    verdict = check(item(title="Wildberries меняет тариф хранения с 3 сентября"), CFG)
    assert not verdict.stopped
    assert verdict.code is None


def test_every_returned_code_exists_in_config():
    """Правило не может вернуть код, которого нет в config/triage.yaml."""
    codes = CFG.stop_rule_codes()
    samples = [
        item(title="Топ-10 товаров"),
        item(title="Скидки до 70%"),
        item(title="Наш сервис — попробуйте бесплатно"),
        item(title="Говорят, будет рост"),
        item(title="Верь в себя"),
        item(title="Ozon запустил акцию"),
        item(title="Как выбрать чайник"),
        item(published=NOW - timedelta(days=45)),
        item(body="Коротко."),
    ]
    for sample in samples:
        verdict = check(sample, CFG)
        assert verdict.stopped, f"ожидался отсев для {sample.title!r}"
        assert verdict.code in codes, f"{verdict.code} нет в config/triage.yaml"


def test_stopped_verdict_always_carries_detail():
    verdict = check(item(title="Топ-10 товаров"), CFG)
    assert verdict.detail
