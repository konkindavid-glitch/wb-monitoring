"""Опрос нажатий: поведение при недоступном Телеграме.

В контейнере api.telegram.org отвечал ENETUNREACH — мгновенно, без попытки
соединиться. Цикл повторял запрос по разу в секунду и залил лог полусотней
одинаковых строк в минуту, ничего этим не добившись.
"""
import app
from monitoring import net


class Clock:
    """Часы и сон под контролем: тест не должен ждать по-настоящему."""

    def __init__(self):
        self.now, self.slept = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def run_poll(monkeypatch, replies, seconds=60):
    clock = Clock()
    calls = []

    def fake_updates(token, offset, timeout=25):
        calls.append(timeout)
        # Отказ возвращается мгновенно, успех «съедает» свой таймаут.
        result = replies.pop(0) if replies else None
        clock.now += 0.0 if result is None else timeout
        return result

    monkeypatch.setattr(app.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(app.time, "sleep", clock.sleep)
    monkeypatch.setattr(app, "get_updates", fake_updates)
    monkeypatch.setattr(app, "read_offset", lambda: 0)
    monkeypatch.setattr(app, "write_offset", lambda value: None)

    app.poll_moderation(app.Deps(cfg=None, token="T", chat_id="1"), seconds)
    return calls, clock


def test_unreachable_telegram_is_not_hammered(monkeypatch):
    """Раньше выходило около шестидесяти запросов в минуту в никуда."""
    calls, _ = run_poll(monkeypatch, [])
    assert len(calls) <= 10


def test_backoff_grows_instead_of_staying_at_one_second(monkeypatch):
    _, clock = run_poll(monkeypatch, [])
    assert clock.slept[0] == 1.0
    assert clock.slept[1] == 2.0
    assert max(clock.slept) > 4


def test_backoff_never_outlives_the_polling_window(monkeypatch):
    """Пауза не должна выходить за отведённое окно: иначе тик задержится."""
    _, clock = run_poll(monkeypatch, [], seconds=10)
    assert sum(clock.slept) <= 10


def test_successful_poll_resets_the_backoff(monkeypatch):
    """Связь восстановилась — реакция обязана снова стать мгновенной."""
    _, clock = run_poll(monkeypatch, [None, None, [], None])
    assert clock.slept[:2] == [1.0, 2.0]
    assert clock.slept[2] == 1.0


def test_updates_are_dispatched_by_type(monkeypatch):
    seen = []
    monkeypatch.setattr(app, "handle_callback",
                        lambda q, d: seen.append("callback"))
    monkeypatch.setattr(app, "handle_message", lambda m, d: seen.append("message"))
    monkeypatch.setattr(app, "handle_channel_post",
                        lambda p, d: seen.append("channel"))
    run_poll(monkeypatch, [[
        {"update_id": 1, "callback_query": {}},
        {"update_id": 2, "message": {}},
        {"update_id": 3, "channel_post": {}},
    ]])
    assert seen == ["callback", "message", "channel"]


def test_one_broken_update_does_not_stop_the_rest(monkeypatch):
    seen = []

    def boom(query, deps):
        raise RuntimeError("сломалось")

    monkeypatch.setattr(app, "handle_callback", boom)
    monkeypatch.setattr(app, "handle_message", lambda m, d: seen.append("message"))
    run_poll(monkeypatch, [[
        {"update_id": 1, "callback_query": {}},
        {"update_id": 2, "message": {}},
    ]])
    assert seen == ["message"]


# --- IPv4 ------------------------------------------------------------------

def test_client_is_bound_to_ipv4():
    """У api.telegram.org есть AAAA-запись, а маршрута для IPv6 в контейнере
    нет: ядро отвечает ENETUNREACH сразу. Привязка к 0.0.0.0 оставляет
    только IPv4-адреса."""
    client = net.ipv4_client(5.0)
    transport = client._transport
    assert transport._pool._local_address == net.IPV4_ANY
    client.close()


def test_ipv4_binding_failure_still_yields_a_working_client(monkeypatch, capsys):
    """Клиент нужен в любом случае: без него не будет и сообщения о том,
    что что-то не так."""
    original = net.httpx.HTTPTransport
    calls = {"n": 0}

    def maybe_fail(*a, **kw):
        calls["n"] += 1
        if kw.get("local_address"):
            raise OSError("нельзя привязаться")
        return original(*a, **kw)

    monkeypatch.setattr(net.httpx, "HTTPTransport", maybe_fail)
    client = net.ipv4_client(5.0)
    assert client is not None
    assert "IPv4-привязка недоступна" in capsys.readouterr().out
    client.close()
