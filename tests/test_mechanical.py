from datetime import datetime, timedelta, timezone
from pathlib import Path

from monitoring.config import load_config
from monitoring.factors.mechanical import mechanical_factors
from monitoring.models import SourceItem

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT)
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def item(**kw):
    base = dict(source_key="s", url="https://x.invalid/a", url_hash="h",
                title="Заголовок", body="Тело", discovered_at=NOW,
                published_at=NOW, tier="T3", platform="CROSS_PLATFORM")
    base.update(kw)
    return SourceItem(**base)


def f(it, known=frozenset(), sources=1):
    return mechanical_factors(it, CFG, known_urls=set(known),
                              independent_sources=sources)


def test_wb_platform_fires():
    assert "platform_wb" in f(item(platform="WILDBERRIES"))


def test_ozon_does_not_fire_wb_factor():
    assert "platform_wb" not in f(item(platform="OZON"))


def test_t1_source_is_authoritative_and_has_no_penalty():
    got = f(item(tier="T1"))
    assert "authoritative_source" in got
    assert "no_confirmation" not in got


def test_t5_single_source_gets_no_confirmation_penalty():
    got = f(item(tier="T5"))
    assert "no_confirmation" in got
    assert "authoritative_source" not in got


def test_two_independent_sources_remove_the_penalty():
    assert "no_confirmation" not in f(item(tier="T5"), sources=2)


def test_fresh_and_old_are_mutually_exclusive():
    fresh = f(item(published_at=NOW - timedelta(hours=2)))
    old = f(item(published_at=NOW - timedelta(days=20)))
    assert "is_fresh" in fresh and "is_old" not in fresh
    assert "is_old" in old and "is_fresh" not in old


def test_item_without_date_gets_neither_fresh_nor_old():
    got = f(item(published_at=None))
    assert "is_fresh" not in got
    assert "is_old" not in got


def test_known_url_is_repeat():
    assert "is_repeat" in f(item(url_hash="seen"), known={"seen"})


def test_ai_keywords_fire_ai_link():
    assert "ai_link" in f(item(title="Нейросеть для карточек товаров"))
    assert "ai_link" in f(item(title="Новый AI-помощник для селлеров"))


def test_every_returned_key_is_a_real_factor_with_rationale():
    got = f(item(platform="WILDBERRIES", tier="T1",
                 title="Нейросеть для карточек"), sources=2)
    assert set(got) <= set(CFG.factor_weights())
    assert all(v and v.strip() for v in got.values())


# --- кому полагается штраф за неподтверждённость ----------------------------

def item_from(tier, **kw):
    from datetime import datetime, timezone
    from monitoring.models import SourceItem
    return SourceItem(source_key="s", url="https://x.invalid/1", url_hash="h",
                      title="Wildberries меняет тариф хранения",
                      body="Ставка вырастет с 3 сентября.",
                      discovered_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
                      tier=tier, **kw)


def fired_for(tier, sources=1):
    return mechanical_factors(item_from(tier), None, known_urls=set(),
                              independent_sources=sources)


def test_trade_media_gets_neither_bonus_nor_penalty():
    """Публикация в отраслевом издании с редактурой — не слух. На боевых
    данных −50 доставался каждой находке: 175 из 176 ушли в DROP."""
    for tier in ("T2", "T3"):
        fired = fired_for(tier)
        assert "no_confirmation" not in fired, tier
        assert "authoritative_source" not in fired, tier


def test_official_feed_is_authoritative():
    """Первоисточник подтверждать нечем и незачем."""
    assert "authoritative_source" in fired_for("T1")
    assert "no_confirmation" not in fired_for("T1")


def test_unverified_sources_still_pay_the_penalty():
    """Штраф задуман против слухов — соцсети, форумы, блоги."""
    for tier in ("T4", "T5", "T6"):
        assert "no_confirmation" in fired_for(tier), tier


def test_confirmation_removes_the_penalty_for_unverified():
    """Одна и та же новость из двух независимых источников — уже не слух."""
    assert "no_confirmation" not in fired_for("T5", sources=2)
