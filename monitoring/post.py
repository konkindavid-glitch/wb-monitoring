"""Карточка находки для отправки редактору.

Это НЕ готовый пост в канал. Пост требует проверенных фактов, расчёта эффекта
и вывода «что делать» — всё это живёт в слоях, которых в v1 нет. Карточка
показывает ровно то, что система знает наверняка: что нашли, где, по каким
темам и почему столько баллов.

Разбор по факторам в карточке обязателен. Редактор должен видеть не «130»,
а из чего эти 130 сложились: иначе оценке невозможно ни доверять, ни спорить
с ней, а калибровать пороги — тем более.
"""

TELEGRAM_LIMIT = 4096

BAND_MARKS = {
    "URGENT": "🔴 срочно",
    "QUEUE": "🟡 в работу",
    "BACKLOG": "⚪ в запас",
    "DROP": "⚫ отброшено",
}

PLATFORM_NAMES = {
    "WILDBERRIES": "Wildberries",
    "OZON": "Ozon",
    "YANDEX_MARKET": "Яндекс Маркет",
    "CROSS_PLATFORM": "рынок целиком",
}

TOPIC_NAMES = {
    "seller_money": "деньги селлеров",
    "rules_offer": "правила и оферты",
    "advertising": "реклама",
    "logistics_warehouse": "логистика и склады",
    "algorithms_tech": "алгоритмы",
    "ai_marketplace": "AI на маркетплейсах",
    "regulation": "регулирование",
    "disputes": "споры и суды",
    "seller_cases": "кейсы селлеров",
    "incidents_scandals": "проблемы и скандалы",
    "market_trends": "тренды рынка",
}


def _fired(hit: dict) -> list:
    factors = hit.get("factors") or {}
    fired = [(k, v) for k, v in factors.items() if v.get("hit")]
    return sorted(fired, key=lambda kv: -abs(kv[1].get("weight", 0)))


def format_card(hit: dict, *, is_test: bool = False) -> str:
    """Карточка находки. is_test помечает сообщение как проверочное."""
    lines = []

    if is_test:
        lines += ["🧪 ПРОВЕРКА СВЯЗИ", "",
                  "Это тестовая карточка: проверяем, что сообщения доходят "
                  "до бота. Постов система пока не пишет.", ""]

    band = BAND_MARKS.get(hit.get("decision"), hit.get("decision", "?"))
    lines.append(f"{band} · {hit.get('score', '?')} баллов")
    lines.append("")
    lines.append(hit.get("title", "без заголовка"))

    platforms = [PLATFORM_NAMES.get(p, p) for p in (hit.get("platforms") or [])]
    topics = [TOPIC_NAMES.get(t, t) for t in (hit.get("topics") or [])]
    if platforms or topics:
        lines.append("")
        if platforms:
            lines.append(f"Площадка: {', '.join(platforms)}")
        if topics:
            lines.append(f"Темы: {', '.join(topics)}")

    fired = _fired(hit)
    if fired:
        lines += ["", "Из чего сложилась оценка:"]
        for key, value in fired:
            weight = value.get("weight", 0)
            sign = "+" if weight > 0 else ""
            why = value.get("why", "")
            lines.append(f"  {sign}{weight} · {why}" if why
                         else f"  {sign}{weight} · {key}")

    if hit.get("url"):
        lines += ["", hit["url"]]

    lines += ["", "Баллы — это очередь, а не разрешение публиковать."]
    return "\n".join(lines)[:TELEGRAM_LIMIT]


def sample_card() -> dict:
    """Эталонная находка для проверки связи, когда база ещё пуста.

    Числа взяты из docs/01-triage-scoring.md §2.3 — того самого случая,
    на котором держатся тесты матрицы.
    """
    return {
        "hit_id": "hit_sample",
        "title": "Wildberries меняет тариф хранения с 3 сентября",
        "url": "https://seller.wildberries.ru/tariffs",
        "score": 130,
        "decision": "URGENT",
        "platforms": ["WILDBERRIES"],
        "topics": ["seller_money", "logistics_warehouse"],
        "factors": {
            "platform_wb": {"hit": True, "weight": 25, "why": "раздел тарифов WB"},
            "seller_money_impact": {"hit": True, "weight": 25,
                                    "why": "прямые расходы на хранение"},
            "rules_change": {"hit": True, "weight": 20,
                             "why": "новая редакция тарифной таблицы"},
            "authoritative_source": {"hit": True, "weight": 15,
                                     "why": "официальный раздел площадки"},
            "is_fresh": {"hit": True, "weight": 15,
                         "why": "обнаружено через 6 минут"},
            "has_practical_takeaway": {"hit": True, "weight": 15,
                                       "why": "пересчёт себестоимости хранения"},
            "mass_effect": {"hit": True, "weight": 15,
                            "why": "затронуты все FBO-поставки"},
        },
    }
