"""Отчёт о состоянии бота — прямо в чат по команде.

Логи Амверы с машины разработки недоступны: сеть рвёт соединение до
`cloud.amvera.ru`. Когда нажатие кнопки не даёт результата, отличить
«не доехал новый код» от «Телеграм не отдаёт обновления» и от «модель
без денег» по молчанию невозможно.

Поэтому бот отвечает на команду сам. Единственный канал, который заведомо
работает, — тот, по которому пришёл вопрос.

Отдельно про вебхук. Пока у бота установлен вебхук, getUpdates не отдаёт
ни одного обновления и отвечает 409. Кнопки при этом мертвы полностью,
а внешне это неотличимо от «никто ничего не нажимал».
"""

OK = "✅"
BAD = "⚠️"


def _line(ok: bool, label: str, detail: str) -> str:
    return f"{OK if ok else BAD} {label}: {detail}"


def format_status(facts: dict) -> str:
    """Отчёт по собранным фактам. Чистая функция — сеть живёт снаружи."""
    lines = ["🩺 Состояние бота", "", f"Сборка: {facts.get('build', '—')}", ""]

    bot = facts.get("bot_username")
    lines.append(_line(bool(bot), "токен",
                       f"@{bot}" if bot else "Телеграм не признал токен"))

    webhook = facts.get("webhook_url") or ""
    lines.append(_line(
        not webhook, "вебхук",
        "не задан, опрос работает" if not webhook
        else f"ЗАДАН ({webhook}) — из-за него кнопки не работают вовсе"))

    db = facts.get("db_error")
    lines.append(_line(not db, "база", "отвечает" if not db else str(db)))

    model = facts.get("model")
    lines.append(_line(bool(model), "модель",
                       model or "ключа нет, посты писать нечем"))

    channel = facts.get("channel")
    lines.append(_line(
        True, "канал",
        channel if channel else "не задан — посты приходят сюда"))

    tick = facts.get("last_tick")
    lines.append(_line(bool(tick), "последний сбор",
                       tick or "ещё не было с момента запуска"))

    presses = facts.get("presses")
    lines.append(_line(True, "нажатий обработано", str(presses)
                       if presses is not None else "—"))

    if webhook:
        lines += ["", "Чтобы кнопки заработали, вебхук надо снять — "
                      "он перехватывает все обновления."]

    return "\n".join(lines)
