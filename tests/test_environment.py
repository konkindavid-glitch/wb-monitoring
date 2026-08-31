"""Проверка окружения на старте.

Без DATABASE_URL сервис падал на первой строке, Амвера поднимала контейнер
заново, и лог заполнялся одинаковыми трейсбеками — при том, что за перезапуски
капают деньги. Настроенность должна проверяться до подключений.
"""
from app import check_environment


def test_all_set_means_nothing_missing(monkeypatch):
    for name in ("DATABASE_URL", "OPENROUTER_API_KEY",
                 "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(name, "x")
    assert check_environment(dry_run=False) == []


def test_missing_database_url_is_reported(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name in ("OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(name, "x")
    missing = check_environment(dry_run=False)
    assert any(m.startswith("DATABASE_URL") for m in missing)


def test_dry_run_does_not_require_database(monkeypatch):
    """Сухой прогон работает без базы — он для того и нужен."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name in ("OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(name, "x")
    assert check_environment(dry_run=True) == []


def test_either_llm_key_satisfies_the_check(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "x")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert check_environment(dry_run=False) == []


def test_missing_llm_key_is_reported_with_its_consequence(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "x")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    missing = check_environment(dry_run=False)
    assert len(missing) == 1
    assert "DROP" in missing[0], "сообщение должно объяснять последствие"


def test_half_configured_telegram_counts_as_missing(monkeypatch):
    """Токен без chat_id бесполезен — отправлять некуда."""
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert any("TELEGRAM" in m for m in check_environment(dry_run=False))


# --- источник подключения к базе ------------------------------------------

from app import database_source  # noqa: E402


def test_database_url_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    monkeypatch.setenv("DB_PASSWORD", "секрет")
    kind, source = database_source()
    assert kind == "url"
    assert source == "postgresql://u:p@h:5432/d"


def test_db_password_alone_is_enough(monkeypatch):
    """Пароль со спецсимволами не нужно экранировать — в этом весь смысл."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_PASSWORD", "па@роль:с/спец#символами%")
    kind, parts = database_source()
    assert kind == "parts"
    assert parts["password"] == "па@роль:с/спец#символами%"
    assert parts["host"] == "amvera-davidkonkin-cnpg-monitoring-db-rw"
    assert parts["user"] == "monitoring"
    assert parts["dbname"] == "monitoring"


def test_db_parts_can_be_overridden(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_PASSWORD", "p")
    monkeypatch.setenv("DB_HOST", "other-host")
    monkeypatch.setenv("DB_NAME", "other-db")
    _, parts = database_source()
    assert parts["host"] == "other-host"
    assert parts["dbname"] == "other-db"


def test_nothing_set_means_no_source(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    assert database_source() == (None, None)
