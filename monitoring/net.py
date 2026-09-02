"""HTTP-клиенты для внешних сервисов.

Контейнер в Амвере переезжает между узлами, и сеть на них разная. Измерено
`monitoring/netcheck.py` на двух запусках подряд:

  - узел A: у контейнера есть IPv6-адрес, но маршрута наружу нет. Обычный
    клиент выбирает IPv6 Телеграма и падает с `ENETUNREACH`. Привязка к IPv4
    делает хуже: резолвинг с `AF_INET` не возвращает ничего (`EAI_FAMILY`);
  - узел B: IPv6 нет вовсе, IPv4 = 10.112.131.135. Обычный клиент работает.

Отсюда `plain_client` и `ipv4_client` — перебираются в delivery, а не
выбираются заранее. Гипотеза про NAT64, на которой строилась предыдущая
версия этого файла, измерением опровергнута: `ipv4only.arpa` синтезированных
AAAA не отдаёт, трансляции нет.
"""
import httpx

IPV4_ANY = "0.0.0.0"


def plain_client(timeout: float, headers: dict = None) -> httpx.Client:
    """Обычный клиент: семейство адресов выбирает система."""
    return httpx.Client(timeout=timeout, headers=headers or {})


def ipv4_client(timeout: float, headers: dict = None) -> httpx.Client:
    """Клиент, привязанный к IPv4. При отказе привязки — обычный клиент."""
    try:
        return httpx.Client(
            transport=httpx.HTTPTransport(local_address=IPV4_ANY),
            timeout=timeout, headers=headers or {})
    except Exception as exc:
        print(f"[degraded] IPv4-привязка недоступна ({exc}), клиент обычный")
        return httpx.Client(timeout=timeout, headers=headers or {})
