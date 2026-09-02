"""Что контейнер может в сети, а чего не может.

Три попытки объяснить недоступность Телеграма были догадками, и каждая
стоила пересборки. Эта проверка печатает факты — и первым же запуском
опровергла последнюю из догадок (NAT64 нет, IPv4 у контейнера есть).

Что она показала на разных узлах:

    узел A   IPv6 есть, маршрута нет   → ENETUNREACH на api.telegram.org
    узел B   IPv6 нет, IPv4 есть       → всё работает

То есть дело не в коде и не в Телеграме, а в том, на какой узел попал
контейнер. Поэтому строки ниже печатаются при каждом старте: когда бот
снова замолчит, ответ будет в логе сразу, а не через три пересборки.
"""
import socket

# RFC 7050: имя существует только в IPv4. Если резолвер отдаёт на него AAAA,
# это и есть синтезированный адрес, из которого читается префикс NAT64.
WELL_KNOWN_IPV4_ONLY = "ipv4only.arpa"
TELEGRAM_HOST = "api.telegram.org"


def _families(host: str, port: int = 443) -> list:
    """Адреса host по семействам. Ошибка возвращается строкой, а не бросается."""
    out = []
    for family, label in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
        try:
            infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
            out.append((label, sorted({i[4][0] for i in infos})))
        except socket.gaierror as exc:
            out.append((label, f"нет ({exc.strerror or exc})"))
    return out


def local_addresses() -> list:
    """Свои адреса. Пустой IPv4 объясняет, почему A-записи не выбираются."""
    found = []
    for family, label in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
        sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            # UDP-connect не шлёт пакетов: ядро лишь выбирает исходящий адрес,
            # и по нему видно, есть ли вообще маршрут этого семейства.
            sock.connect(("8.8.8.8" if family == socket.AF_INET else "2001:4860:4860::8888", 53))
            found.append((label, sock.getsockname()[0]))
        except OSError as exc:
            found.append((label, f"нет ({exc.strerror or exc})"))
        finally:
            sock.close()
    return found


def nat64_prefix() -> str:
    """Префикс NAT64 или пустая строка. По RFC 7050."""
    try:
        infos = socket.getaddrinfo(WELL_KNOWN_IPV4_ONLY, None,
                                   socket.AF_INET6, socket.SOCK_STREAM)
    except socket.gaierror:
        return ""
    for info in infos:
        address = info[4][0]
        # Синтезированный адрес несёт 192.0.0.170 в последних 32 битах.
        packed = socket.inet_pton(socket.AF_INET6, address)
        if packed[-4:] in (bytes([192, 0, 0, 170]), bytes([192, 0, 0, 171])):
            return socket.inet_ntop(socket.AF_INET6, packed[:12] + b"\0\0\0\0")
    return ""


def report() -> list:
    """Строки для лога. Печатает вызывающий — модулю не место в выводе."""
    lines = ["[net] собственные адреса: " +
             ", ".join(f"{label}={value}" for label, value in local_addresses())]

    for host in (TELEGRAM_HOST, WELL_KNOWN_IPV4_ONLY):
        for label, value in _families(host):
            shown = value if isinstance(value, str) else ", ".join(value)
            lines.append(f"[net] {host} {label}: {shown}")

    prefix = nat64_prefix()
    lines.append(f"[net] NAT64: {prefix + '/96' if prefix else 'не обнаружен'}")
    return lines
