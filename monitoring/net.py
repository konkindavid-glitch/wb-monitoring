"""HTTP-клиент, ходящий только по IPv4.

В контейнере Амверы `api.telegram.org` недоступен: каждый запрос мгновенно
падает с `[Errno 101] Network is unreachable`. При этом RSS-источники
забираются исправно — 222 материала за прогон, — то есть сеть работает.

Разница в адресах. У `api.telegram.org` есть AAAA-запись, httpx выбирает
IPv6-адрес, а маршрута для IPv6 в контейнере нет: ядро отвечает ENETUNREACH
сразу, не пытаясь соединиться. Отсюда и мгновенность отказа — на таймаут
это не похоже. Российские отраслевые сайты живут на IPv4 и потому работают.

Привязка сокета к `0.0.0.0` заставляет выбирать только IPv4-адреса.
Не подменяет диагностику: если IPv4 тоже не будет работать, ошибка станет
другой и попадёт в лог как есть.
"""
import httpx

IPV4_ANY = "0.0.0.0"


def ipv4_client(timeout: float, headers: dict = None) -> httpx.Client:
    """Клиент, привязанный к IPv4. При отказе привязки — обычный клиент."""
    try:
        return httpx.Client(
            transport=httpx.HTTPTransport(local_address=IPV4_ANY),
            timeout=timeout, headers=headers or {})
    except Exception as exc:
        print(f"[degraded] IPv4-привязка недоступна ({exc}), клиент обычный")
        return httpx.Client(timeout=timeout, headers=headers or {})
