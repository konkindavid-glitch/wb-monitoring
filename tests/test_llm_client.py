"""Выбор клиента модели и разбор ответа OpenRouter."""
import json

import pytest

from monitoring.factors import judgment


def test_openrouter_wins_when_both_keys_are_set(monkeypatch):
    """Один ключ на все модели и никаких блокировок — приоритет за ним."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert isinstance(judgment.build_client(), judgment.OpenRouterClient)


def test_falls_back_to_anthropic_when_only_that_key_is_set(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # anthropic может быть не установлен локально — важно лишь, что не OpenRouter
    try:
        client = judgment.build_client()
    except ImportError:
        pytest.skip("пакет anthropic не установлен")
    assert not isinstance(client, judgment.OpenRouterClient)


def test_no_keys_yields_none_so_the_tick_can_shout(monkeypatch):
    """None — сигнал тику кричать о деградации, а не молча занижать оценки."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert judgment.build_client() is None


def test_model_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.0-flash-001")
    assert judgment.build_client()._model == "google/gemini-2.0-flash-001"


def test_openrouter_sends_expected_payload_and_reads_the_answer(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    sent = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"a": {}}'}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["url"] = url
        sent["headers"] = headers
        sent["json"] = json
        return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    client = judgment.OpenRouterClient("anthropic/claude-haiku-4.5")
    assert client.complete("промпт") == '{"a": {}}'
    assert sent["url"] == judgment.OpenRouterClient.URL
    assert sent["headers"]["Authorization"] == "Bearer or-test"
    assert sent["json"]["model"] == "anthropic/claude-haiku-4.5"
    assert sent["json"]["messages"][0]["content"] == "промпт"


def test_openrouter_failure_is_caught_by_judgment_factors(monkeypatch):
    """Отказ роутера не роняет тик, но обязан попасть в stats."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")

    import httpx

    def failing_post(*a, **kw):
        raise httpx.HTTPError("роутер недоступен")

    monkeypatch.setattr(httpx, "post", failing_post)

    from datetime import datetime, timezone
    from monitoring.models import SourceItem
    item = SourceItem(source_key="s", url="https://x/1", url_hash="a",
                      title="Т", body="Т",
                      discovered_at=datetime(2026, 8, 27, tzinfo=timezone.utc))

    stats = {}
    assert judgment.judgment_factors([item], judgment.OpenRouterClient(),
                                     stats=stats) == {"a": {}}
    assert stats["failed"] == 1
    assert "роутер недоступен" in stats["error"]
