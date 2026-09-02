"""Отчёт о состоянии. Он нужен ровно тогда, когда всё молчит,
поэтому обязан говорить прямо, а не бодро отчитываться об «ОК».
"""
import app
from monitoring.health import format_status

GOOD = {
    "build": "2026-09-02 · разборы и рисованные обложки",
    "bot_username": "wb_monitor_bot",
    "webhook_url": "",
    "db_error": "",
    "model": "anthropic/claude-haiku-4.5",
    "channel": "@sellers",
    "last_tick": "класс B, {'fetched': 222}",
    "presses": 3,
}


def test_healthy_report_names_the_build():
    """Первое, что нужно знать при «ничего не сработало», — доехал ли код."""
    assert "2026-09-02" in format_status(GOOD)


def test_webhook_is_called_out_as_the_reason_buttons_are_dead():
    """Пока вебхук стоит, getUpdates отвечает 409 и кнопки мертвы целиком,
    а выглядит это как «никто не нажимал»."""
    text = format_status(dict(GOOD, webhook_url="https://example.invalid/hook"))
    assert "ЗАДАН" in text
    assert "не работают" in text
    assert "снять" in text.lower()


def test_absent_webhook_is_reported_as_fine():
    assert "опрос работает" in format_status(GOOD)


def test_broken_database_is_shown_with_its_error():
    text = format_status(dict(GOOD, db_error="connection is closed"))
    assert "connection is closed" in text


def test_missing_model_says_posts_cannot_be_written():
    text = format_status(dict(GOOD, model=""))
    assert "писать нечем" in text


def test_missing_channel_explains_where_posts_go():
    text = format_status(dict(GOOD, channel=""))
    assert "приходят сюда" in text


def test_bad_token_is_reported():
    text = format_status(dict(GOOD, bot_username=""))
    assert "не признал токен" in text


def test_presses_counter_separates_no_press_from_lost_result():
    """Ноль нажатий и «нажатие пришло, но результат не дошёл» —
    разные неисправности, и по молчанию их не различить."""
    assert "нажатий обработано: 0" in format_status(dict(GOOD, presses=0))


def test_missing_facts_do_not_crash_the_report():
    assert format_status({})


# --- команда в чате ---------------------------------------------------------

class Repo:
    def degraded_sources(self):
        return []


def test_status_command_answers_in_the_chat(monkeypatch):
    replies = []
    monkeypatch.setattr(app, "send",
                        lambda text, token, chat: replies.append((text, chat)))
    monkeypatch.setattr(app, "collect_status", lambda deps: GOOD)

    deps = app.Deps(cfg=None, repo=Repo(), token="T", chat_id="100")
    app.handle_message({"text": "/статус", "chat": {"id": 100}}, deps)

    assert replies and "Состояние бота" in replies[0][0]


def test_status_command_works_with_the_bot_suffix(monkeypatch):
    """В группе Телеграм дописывает к команде имя бота."""
    replies = []
    monkeypatch.setattr(app, "send",
                        lambda text, token, chat: replies.append(text))
    monkeypatch.setattr(app, "collect_status", lambda deps: GOOD)

    app.handle_message({"text": "/status@wb_monitor_bot", "chat": {"id": 100}},
                       app.Deps(cfg=None, repo=Repo(), token="T", chat_id="1"))
    assert replies


def test_ordinary_message_is_not_treated_as_a_command(monkeypatch):
    replies = []
    monkeypatch.setattr(app, "send",
                        lambda text, token, chat: replies.append(text))
    app.handle_message({"text": "привет", "chat": {"id": 100}},
                       app.Deps(cfg=None, repo=Repo(), token="T", chat_id="1"))
    assert replies == []


def test_collect_status_survives_a_dead_database(monkeypatch):
    """Отчёт нужен именно когда что-то сломано — падать ему нельзя."""
    class Dead:
        def degraded_sources(self):
            raise RuntimeError("connection refused")

        def rollback(self):
            pass

    monkeypatch.setattr("monitoring.delivery.bot_identity", lambda t: {})
    monkeypatch.setattr("monitoring.delivery.webhook_info", lambda t: {})

    facts = app.collect_status(app.Deps(cfg=None, repo=Dead(), token="T"))
    assert "connection refused" in facts["db_error"]
