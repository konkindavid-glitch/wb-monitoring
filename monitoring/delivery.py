"""Доставка в Telegram: срочные находки сразу, остальное дайджестом.

Срочная находка уходит карточкой из monitoring/post.py — с обложкой и тремя
кнопками решения. Отдельного формата для срочного здесь нет намеренно:
две разные вёрстки одного и того же для одного и того же редактора неминуемо
разъезжаются, что уже случилось с подключением к базе.

Дайджест — слой отчётности: список того, что ждёт в очереди. Кнопок в нём нет,
решения принимаются на карточке.

Пустые тики не отправляются. Тридцать сообщений «нового нет» подряд приучают
не открывать бота вернее, чем отсутствие мониторинга.
"""
import json

import httpx

TELEGRAM_LIMIT = 4096
API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 20.0


def _fired(hit: dict) -> list:
    factors = hit.get("factors") or {}
    return [(key, value["why"]) for key, value in factors.items()
            if value.get("hit") and value.get("why")]


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


# --- карточка с обложкой и кнопками ---------------------------------------

PHOTO_API = "https://api.telegram.org/bot{token}/sendPhoto"
EDIT_TEXT_API = "https://api.telegram.org/bot{token}/editMessageText"
EDIT_CAPTION_API = "https://api.telegram.org/bot{token}/editMessageCaption"
DELETE_API = "https://api.telegram.org/bot{token}/deleteMessage"
ANSWER_API = "https://api.telegram.org/bot{token}/answerCallbackQuery"
UPDATES_API = "https://api.telegram.org/bot{token}/getUpdates"

# Подпись к фото ограничена 1024 символами, а не 4096, как текст сообщения.
# Разбор по факторам в них не всегда влезает, поэтому длинная карточка уходит
# отдельным сообщением — с кнопками именно на нём.
CAPTION_LIMIT = 1024


def _call(url: str, payload: dict, files: dict = None):
    """Ответ Телеграма или None. Сбой доставки не должен ронять тик."""
    try:
        response = httpx.post(url, data=payload if files else None,
                              json=None if files else payload,
                              files=files, timeout=TIMEOUT)
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return body.get("result") if body.get("ok") else None


def send_card(text: str, token: str, chat_id: str, *,
              cover: bytes = None, reply_markup: dict = None):
    """Карточка: обложка сверху, текст, кнопки. Возвращает message_id или None.

    Кнопки всегда оказываются на сообщении с полным текстом: нажимать их,
    не видя разбора по факторам, редактору не на чем.
    """
    if not token or not chat_id:
        return None

    markup = json.dumps(reply_markup) if reply_markup else None

    if cover and len(text) <= CAPTION_LIMIT:
        payload = {"chat_id": chat_id, "caption": text}
        if markup:
            payload["reply_markup"] = markup
        result = _call(PHOTO_API.format(token=token), payload,
                       files={"photo": ("cover.png", cover, "image/png")})
        return result.get("message_id") if result else None

    if cover:
        _call(PHOTO_API.format(token=token), {"chat_id": chat_id},
              files={"photo": ("cover.png", cover, "image/png")})

    payload = {"chat_id": chat_id, "text": text,
               "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    result = _call(API.format(token=token), payload)
    return result.get("message_id") if result else None


def answer_callback(callback_id: str, token: str, text: str = None) -> bool:
    """Гасит часики на кнопке. Без этого Телеграм крутит их до таймаута."""
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    return _call(ANSWER_API.format(token=token), payload) is not None


def replace_text(message_id: int, text: str, token: str, chat_id: str,
                 *, has_caption: bool = False, reply_markup: dict = None) -> bool:
    """Меняет текст сообщения. Без reply_markup кнопки исчезают.

    У сообщения с фото правится подпись, у обычного — текст. Перепутать
    нельзя: Телеграм отвечает «there is no text in the message to edit».

    Кнопки убираются по умолчанию намеренно: решение принято, и второе
    нажатие не должно опубликовать пост дважды. Вернуть их можно явно —
    это нужно, когда пост не написался и попытку стоит повторить.
    """
    url = EDIT_CAPTION_API if has_caption else EDIT_TEXT_API
    field = "caption" if has_caption else "text"
    payload = {"chat_id": chat_id, "message_id": message_id,
               field: text[:CAPTION_LIMIT if has_caption else TELEGRAM_LIMIT]}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _call(url.format(token=token), payload) is not None


def delete_message(message_id: int, token: str, chat_id: str) -> bool:
    return _call(DELETE_API.format(token=token),
                 {"chat_id": chat_id, "message_id": message_id}) is not None


def get_updates(token: str, offset: int, timeout: int = 25) -> list:
    """Длинный опрос обновлений. Список может быть пустым, None не бывает."""
    if not token:
        return []
    try:
        response = httpx.post(
            UPDATES_API.format(token=token),
            json={"offset": offset, "timeout": timeout,
                  "allowed_updates": ["callback_query", "message"]},
            timeout=timeout + 10)
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    return body.get("result", []) if body.get("ok") else []
