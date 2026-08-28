import json
from datetime import datetime, timezone

from monitoring.factors.judgment import (
    JUDGMENT_KEYS, build_prompt, judgment_factors, parse_response)
from monitoring.models import SourceItem

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def item(h, title="Заголовок"):
    return SourceItem(source_key="s", url=f"https://x.invalid/{h}", url_hash=h,
                      title=title, body="Тело", discovered_at=NOW, published_at=NOW)


class FakeClient:
    """Заглушка: весь тракт тестируется без сети и без счёта за модель."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


def test_parses_factors_for_each_item():
    got = judgment_factors([item("a"), item("b")], FakeClient({
        "a": {"seller_money_impact": "растёт комиссия"},
        "b": {"is_advertising": "реклама сервиса"},
    }))
    assert got["a"] == {"seller_money_impact": "растёт комиссия"}
    assert got["b"] == {"is_advertising": "реклама сервиса"}


def test_drops_keys_outside_the_judgment_set():
    """Модель не должна проставлять механические факторы — они считаются кодом."""
    got = judgment_factors([item("a")], FakeClient({
        "a": {"platform_wb": "WB", "mass_effect": "весь рынок"}}))
    assert got["a"] == {"mass_effect": "весь рынок"}


def test_drops_factors_without_rationale():
    got = judgment_factors([item("a")], FakeClient({
        "a": {"mass_effect": "", "rules_change": "меняется оферта"}}))
    assert got["a"] == {"rules_change": "меняется оферта"}


def test_drops_ids_the_model_invented():
    got = judgment_factors([item("a")], FakeClient({
        "a": {"mass_effect": "рынок"}, "z": {"mass_effect": "нет такого id"}}))
    assert set(got) == {"a"}


def test_batches_are_split():
    client = FakeClient({})
    judgment_factors([item(str(i)) for i in range(32)], client, batch_size=15)
    assert client.calls == 3


def test_invalid_json_yields_empty_factors_not_a_crash():
    class Broken:
        def complete(self, prompt):
            return "не json"

    assert judgment_factors([item("a")], Broken()) == {"a": {}}


def test_model_failure_does_not_break_the_tick():
    class Failing:
        def complete(self, prompt):
            raise RuntimeError("API недоступен")

    assert judgment_factors([item("a")], Failing()) == {"a": {}}


def test_model_failure_is_reported_in_stats():
    """Молчаливый отказ классификатора неотличим от «новостей нет».

    Без семи факторов-суждений материал набирает максимум 65 из 160, и всё
    уходит в DROP. Отказ обязан быть видимым.
    """
    class Failing:
        def complete(self, prompt):
            raise RuntimeError("API key is invalid")

    stats = {}
    judgment_factors([item("a"), item("b")], Failing(), batch_size=1, stats=stats)
    assert stats["batches"] == 2
    assert stats["failed"] == 2
    assert "API key is invalid" in stats["error"]


def test_successful_run_reports_no_failures():
    stats = {}
    judgment_factors([item("a")], FakeClient({"a": {"mass_effect": "рынок"}}),
                     stats=stats)
    assert stats["failed"] == 0
    assert stats["error"] is None


def test_prompt_contains_every_item_id():
    items = [item("aa"), item("bb")]
    prompt = build_prompt(items)
    assert "aa" in prompt and "bb" in prompt


def test_judgment_keys_do_not_overlap_mechanical():
    mechanical = {"platform_wb", "is_fresh", "is_old", "authoritative_source",
                  "is_repeat", "no_confirmation", "ai_link"}
    assert JUDGMENT_KEYS & mechanical == set()
    assert len(JUDGMENT_KEYS) == 7
