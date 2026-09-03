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
    monkeypatch.setattr(app, "_BACKOFF", {"seconds": 1.0})
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


# --- выбор пути до Телеграма -----------------------------------------------

import httpx
import pytest

from monitoring import delivery


@pytest.fixture(autouse=False)
def clean_clients(monkeypatch):
    monkeypatch.setattr(delivery, "_CLIENTS", {})
    monkeypatch.setattr(delivery, "_WORKING", None)


class Stub:
    def __init__(self, works):
        self.works, self.calls = works, 0

    def post(self, url, **kw):
        self.calls += 1
        if not self.works:
            raise httpx.ConnectError("нет маршрута")
        return "ответ"


def install(monkeypatch, by_family):
    monkeypatch.setattr(delivery, "_CLIENTS", dict(by_family))
    monkeypatch.setattr(delivery, "_WORKING", None)
    return by_family


def test_falls_back_to_ipv4_when_the_default_path_is_dead(monkeypatch, capsys):
    """Жёсткая привязка к IPv4 была ошибкой — она ломала и то, что работало.
    Пробовать надо оба пути."""
    stubs = install(monkeypatch, {False: Stub(False), True: Stub(True)})
    assert delivery._post("https://x/botT/getMe") == "ответ"
    assert stubs[True].calls == 1
    assert "IPv4" in capsys.readouterr().out


def test_default_path_is_preferred_when_it_works(monkeypatch):
    stubs = install(monkeypatch, {False: Stub(True), True: Stub(False)})
    assert delivery._post("https://x/botT/getMe") == "ответ"
    assert stubs[True].calls == 0


def test_working_path_is_remembered(monkeypatch):
    """Пробовать оба пути на каждом запросе — удваивать задержку впустую."""
    stubs = install(monkeypatch, {False: Stub(False), True: Stub(True)})
    for _ in range(3):
        delivery._post("https://x/botT/getMe")
    assert stubs[False].calls == 1
    assert stubs[True].calls == 3


def test_both_paths_dead_raises_so_the_reason_reaches_the_log(monkeypatch):
    install(monkeypatch, {False: Stub(False), True: Stub(False)})
    with pytest.raises(httpx.HTTPError):
        delivery._post("https://x/botT/getMe")


def test_a_working_path_that_dies_is_retried_from_scratch(monkeypatch):
    """Площадка чинит маршрут — бот обязан ожить сам, без пересборки."""
    dead, alive = Stub(False), Stub(True)
    install(monkeypatch, {False: dead, True: alive})
    delivery._post("https://x/botT/getMe")          # выбран IPv4
    monkeypatch.setattr(delivery, "_CLIENTS", {False: Stub(True), True: Stub(False)})
    with pytest.raises(httpx.HTTPError):
        delivery._post("https://x/botT/getMe")      # запомненный путь отвалился
    assert delivery._post("https://x/botT/getMe") == "ответ"


def test_backoff_survives_the_cycle(monkeypatch):
    """Локальной переменной отступ сбрасывался каждые шестьдесят секунд,
    и при затяжном отказе лог всё равно наполнялся."""
    monkeypatch.setattr(app, "_BACKOFF", {"seconds": 1.0})
    run_poll(monkeypatch, [], seconds=10)
    grown = app._BACKOFF["seconds"]

    monkeypatch.setattr(app, "read_offset", lambda: 0)
    monkeypatch.setattr(app, "get_updates", lambda *a, **k: None)
    assert grown > 1.0


# --- какие полосы уходят карточкой ------------------------------------------

def test_queue_is_delivered_as_a_card_by_default(monkeypatch):
    """Порог в 80 берётся почти только за счёт официальной ленты площадки.
    Пока она отдаёт ноль, при пороге URGENT карточек нет вовсе."""
    monkeypatch.delenv("CARD_BANDS", raising=False)
    assert app.card_bands() == ("URGENT", "QUEUE")


def test_digest_never_overlaps_with_cards(monkeypatch):
    """Иначе одна находка приходит и постом, и строкой в списке,
    и решение по ней принимается дважды."""
    for value in ["URGENT", "URGENT,QUEUE", "URGENT,QUEUE,BACKLOG"]:
        monkeypatch.setenv("CARD_BANDS", value)
        assert not set(app.card_bands()) & set(app.digest_bands())


def test_bands_can_be_narrowed_by_env(monkeypatch):
    monkeypatch.setenv("CARD_BANDS", "URGENT")
    assert app.card_bands() == ("URGENT",)
    assert app.digest_bands() == ("QUEUE", "BACKLOG")


def test_broken_env_falls_back_to_urgent(monkeypatch):
    """Опечатка в переменной не должна отключить доставку совсем."""
    monkeypatch.setenv("CARD_BANDS", "мусор, ерунда")
    assert app.card_bands() == ("URGENT",)


# --- отметка о доставке -----------------------------------------------------

class DeliveryRepo:
    def __init__(self, pending):
        self.pending, self.marked = pending, []

    def pending_cards(self, bands):
        return [h for h in self.pending if h["hit_id"] not in self.marked]

    def pending_digest(self, bands):
        return []

    def degraded_sources(self):
        return []

    def save_heartbeat(self, report, run_id=None):
        pass

    def promote_backlog(self, now, weights, thresholds):
        return []

    def mark_delivered(self, hit_ids):
        self.marked += list(hit_ids)

    def rollback(self):
        pass


def test_post_is_marked_delivered_before_the_next_one_is_sent(monkeypatch):
    """Пачкой отметка терялась при любой ошибке ниже и при перезапуске:
    пост уже ушёл, но помечен не был, и через минуту уходил заново."""
    from datetime import datetime, timezone

    marks_when_sent = []
    repo = DeliveryRepo([
        {"hit_id": "a", "title": "Первая находка про Wildberries", "score": 70,
         "decision": "QUEUE", "platforms": ["WILDBERRIES"], "topics": [],
         "url": "https://x.invalid/a", "factors": {}},
        {"hit_id": "b", "title": "Совсем другая новость про Ozon", "score": 65,
         "decision": "QUEUE", "platforms": ["OZON"], "topics": [],
         "url": "https://x.invalid/b", "factors": {}},
    ])

    def fake_deliver(hit, deps, is_test=False):
        marks_when_sent.append(list(repo.marked))
        return True

    monkeypatch.setattr(app, "deliver_card", fake_deliver)
    monkeypatch.setattr(app, "build_report", lambda state, now: {})

    from monitoring.config import load_config
    deps = app.Deps(cfg=load_config(app.ROOT), repo=repo, token="T", chat_id="1")
    app.run_heartbeat(deps, {"last_run_at": {}, "hits_by_question": {}},
                      datetime(2026, 9, 3, tzinfo=timezone.utc))

    assert repo.marked == ["a", "b"]
    # Ко второй отправке первая уже отмечена — значит отметка идёт сразу.
    assert marks_when_sent[1] == ["a"]
