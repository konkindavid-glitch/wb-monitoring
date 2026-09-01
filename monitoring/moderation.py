"""Кнопки под карточкой и разбор нажатий.

Три решения редактора: запостить, отредактировать, удалить. Здесь только
чистая часть — раскладка клавиатуры, разбор callback_data и текст итога.
Сеть живёт в delivery, запись решения — в db.

Про «Запостить». Модуль не пишет тексты постов: это отдельный слой, которого
в v1 нет (см. README, «Границы модуля»). Поэтому кнопка ставит одобрение
и переводит находку в HANDED_OFF — то есть отдаёт её наружу как согласованную.
Публикацией займётся слой публикации, когда появится. Делать вид, что кнопка
публикует, нельзя: редактор нажмёт и решит, что пост вышел.

Про «Удалить». Решение пишется в базу, а сама находка остаётся: удаляется
только сообщение из чата модерации. Иначе журнал решений врёт — по нему
нельзя ни откалибровать пороги, ни понять, что редактор отклоняет.
"""

PREFIX = "mod"

PUBLISH = "pub"
EDIT = "edit"
REJECT = "del"

BUTTONS = [
    (PUBLISH, "✅ Запостить"),
    (EDIT, "✏️ Редактировать"),
    (REJECT, "🗑 Удалить"),
]

# Telegram режет callback_data на 64 байтах и молча отдаёт BUTTON_DATA_INVALID.
# hit_id — это hit_ плюс 16 hex, с префиксом выходит 28 байт: запас есть,
# но проверка нужна на случай, если формат идентификатора изменится.
CALLBACK_LIMIT = 64

OUTCOME = {
    PUBLISH: "✅ Одобрено к публикации",
    EDIT: "✏️ Отправлено на правку",
    REJECT: "🗑 Отклонено",
}


def callback_data(action: str, hit_id: str) -> str:
    data = f"{PREFIX}:{action}:{hit_id}"
    if len(data.encode("utf-8")) > CALLBACK_LIMIT:
        raise ValueError(f"callback_data длиннее {CALLBACK_LIMIT} байт: {data}")
    return data


def keyboard(hit_id: str) -> dict:
    """Клавиатура под карточкой. Три кнопки в ряд — помещаются на телефоне."""
    return {"inline_keyboard": [[
        {"text": title, "callback_data": callback_data(action, hit_id)}
        for action, title in BUTTONS
    ]]}


def parse_callback(data: str):
    """(действие, hit_id) или None, если нажатие не наше.

    Чужие callback_data игнорируются молча: в одном чате может работать
    не только этот бот, и падать на его кнопках нельзя.
    """
    if not data:
        return None
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != PREFIX:
        return None
    action, hit_id = parts[1], parts[2]
    if action not in dict(BUTTONS) or not hit_id:
        return None
    return action, hit_id


def outcome_text(action: str, card: str) -> str:
    """Текст карточки после решения: итог сверху, карточка под ним.

    Карточка сохраняется целиком — по одной строке «отклонено» через неделю
    невозможно понять, что именно отклонили.
    """
    return f"{OUTCOME[action]}\n\n{card}"


# Короткая подсказка во всплывашке на самой кнопке: редактор видит, что
# нажатие принято, ещё до того как перерисуется сообщение.
OUTCOME_TOAST = {
    PUBLISH: "Одобрено",
    EDIT: "Жду текст правки",
    REJECT: "Отклонено",
}

EDIT_PROMPT = ("Пришлите ответом на это сообщение, что поправить. "
               "Текст сохранится к находке.")
