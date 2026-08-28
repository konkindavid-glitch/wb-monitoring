"""Доставка в Telegram: срочные находки сразу, остальное дайджестом.

Дайджест — слой отчётности, а не доставка редактору из docs/03 §6: кнопок
и решений здесь нет.

Пустые тики не отправляются. Тридцать сообщений «нового нет» подряд приучают
не открывать бота вернее, чем отсутствие мониторинга.
"""
import httpx

TELEGRAM_LIMIT = 4096
API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 20.0


def _fired(hit: dict) -> list:
    factors = hit.get("factors") or {}
    return [(key, value["why"]) for key, value in factors.items()
            if value.get("hit") and value.get("why")]


def format_urgent(hit: dict) -> str:
    lines = [
        f"🔴 {hit['score']} баллов — {hit['decision']}",
        "",
        hit["title"],
        hit["url"],
        "",
        "Сработали факторы:",
    ]
    lines += [f"  • {key}: {why}" for key, why in _fired(hit)]
    lines += ["", "Это приоритет очереди: проверить первым, не публиковать."]
    return "\n".join(lines)[:TELEGRAM_LIMIT]


def format_digest(hits: list, degraded: list, report: dict) -> str:
    head = [
        "📋 Дайджест мониторинга",
        f"Срочных: {report.get('urgent_count', 0)} · "
        f"в очереди: {report.get('queue_count', 0)}",
        "",
    ]

    if not hits and not degraded:
        return "\n".join(head + ["Новых материалов выше порога нет."])

    body = []
    for index, hit in enumerate(hits):
        line = f"• {hit['score']} — {hit['title']}\n  {hit['url']}"
        if len("\n".join(head + body + [line])) > TELEGRAM_LIMIT - 300:
            body.append(f"…и ещё {len(hits) - index} материалов")
            break
        body.append(line)

    tail = []
    if degraded:
        tail = ["", "⚠️ Источники требуют внимания:"]
        tail += [f"  • {name}" for name in degraded]

    return "\n".join(head + body + tail)[:TELEGRAM_LIMIT]


def send(text: str, token: str, chat_id: str) -> bool:
    """Отправка. Неудача не роняет тик — находки останутся неотданными."""
    if not token or not chat_id:
        return False
    try:
        response = httpx.post(
            API.format(token=token),
            json={"chat_id": chat_id, "text": text,
                  "disable_web_page_preview": True},
            timeout=TIMEOUT)
        return response.status_code == 200
    except httpx.HTTPError:
        return False
