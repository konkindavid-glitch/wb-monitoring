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
    """Отправка. Неудача не роняет тик — находки останутся неотданными.

    Идёт через тот же _call, что и всё остальное: своя ветка со своим
    httpx.post означала бы свой клиент мимо IPv4-привязки и своё молчание
    вместо записи причины в лог.
    """
    if not token or not chat_id:
        return False
    return _call(API.format(token=token),
                 {"chat_id": chat_id, "text": text,
                  "disable_web_page_preview": True}) is not None


# --- карточка с обложкой и кнопками ---------------------------------------

PHOTO_API = "https://api.telegram.org/bot{token}/sendPhoto"
EDIT_TEXT_API = "https://api.telegram.org/bot{token}/editMessageText"
EDIT_CAPTION_API = "https://api.telegram.org/bot{token}/editMessageCaption"
DELETE_API = "https://api.telegram.org/bot{token}/deleteMessage"
ANSWER_API = "https://api.telegram.org/bot{token}/answerCallbackQuery"
UPDATES_API = "https://api.telegram.org/bot{token}/getUpdates"
WEBHOOK_INFO_API = "https://api.telegram.org/bot{token}/getWebhookInfo"
ME_API = "https://api.telegram.org/bot{token}/getMe"

# Подпись к фото ограничена 1024 символами, а не 4096, как текст сообщения.
# Разбор по факторам в них не всегда влезает, поэтому длинная карточка уходит
# отдельным сообщением — с кнопками именно на нём.
CAPTION_LIMIT = 1024


_CLIENTS = {}
_WORKING = None

# Последняя жалоба Телеграма. Нужна, чтобы редактор в чате видел причину
# словами Телеграма, а не «не принял»: «bot is not a member of the channel
# chat» говорит, что делать, а «не принял» — нет.
_LAST_ERROR = {"text": ""}


def last_error() -> str:
    return _LAST_ERROR["text"]


def _client(ipv4: bool):
    if ipv4 not in _CLIENTS:
        from monitoring.net import ipv4_client, plain_client
        _CLIENTS[ipv4] = ipv4_client(TIMEOUT) if ipv4 else plain_client(TIMEOUT)
    return _CLIENTS[ipv4]


def _post(url: str, **kwargs):
    """Единственная точка выхода в сеть. Пробует оба пути, помнит рабочий.

    Контейнер переезжает между узлами, и сеть на них разная. На одном узле
    у контейнера был IPv6-адрес без маршрута наружу: обычный клиент выбирал
    IPv6 Телеграма и падал с ENETUNREACH. На другом IPv6 нет вовсе, и обычный
    клиент прекрасно работает по IPv4. Жёсткая привязка к IPv4 не спасала —
    на первом узле она отсекала резолвинг вовсе (EAI_FAMILY).

    Поэтому не выбор семейства, а перебор: сначала обычный клиент, при сетевом
    отказе — привязанный к IPv4. Удачный запоминается, чтобы не платить
    двойной задержкой за каждый запрос, и сбрасывается, если перестал
    работать: тогда после переезда бот оживает сам, без пересборки.

    Через эту же функцию тесты подменяют сеть: подменять httpx.post целиком
    значило бы трогать и коллекторы, которым всё это не нужно.
    """
    global _WORKING
    order = [_WORKING] if _WORKING is not None else [False, True]
    last = None
    for ipv4 in order:
        try:
            response = _client(ipv4).post(url, **kwargs)
        except httpx.HTTPError as exc:
            last = exc
            continue
        if _WORKING is None:
            _WORKING = ipv4
            # Именно «клиент», а не семейство адресов: обычный клиент выбирает
            # семейство сам, и назвать его IPv6 значило бы соврать в логе —
            # ровно та беда, из-за которой этот модуль и переписывался.
            print("[net] Телеграм отвечает: клиент "
                  + ("с привязкой к IPv4" if ipv4 else "обычный"))
        return response
    _WORKING = None
    raise last


def _method(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def _call(url: str, payload: dict, files: dict = None):
    """Ответ Телеграма или None. Сбой доставки не должен ронять тик.

    Причина сбоя обязана попасть в лог. Раньше здесь стоял молчаливый
    return None, и отказ Телеграма выглядел ровно как «всё хорошо, просто
    ничего не пришло»: нажатая кнопка не давала ни результата, ни следа.
    Ошибка, о которой нигде не сказано, — это не обработанная ошибка.
    """
    method = _method(url)
    try:
        response = _post(url, data=payload if files else None,
                         json=None if files else payload, files=files)
        body = response.json()
    except httpx.HTTPError as exc:
        _LAST_ERROR["text"] = f"сеть — {exc}"
        print(f"[telegram] {method}: сеть — {exc}")
        return None
    except ValueError:
        _LAST_ERROR["text"] = "ответ Телеграма не разобрался"
        print(f"[telegram] {method}: ответ не разобрался "
              f"(код {response.status_code})")
        return None

    if not body.get("ok"):
        _LAST_ERROR["text"] = str(body.get("description") or
                                  body.get("error_code"))
        print(f"[telegram] {method}: {body.get('error_code')} "
              f"{body.get('description')}")
        return None
    _LAST_ERROR["text"] = ""
    return body.get("result")


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
                       files={"photo": ("cover.jpg", cover, "image/jpeg")})
        return result.get("message_id") if result else None

    if cover:
        _call(PHOTO_API.format(token=token), {"chat_id": chat_id},
              files={"photo": ("cover.jpg", cover, "image/jpeg")})

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


def get_updates(token: str, offset: int, timeout: int = 25):
    """Длинный опрос обновлений: список при успехе, None при отказе.

    Разница принципиальная. Пустой список значит «никто ничего не нажимал»,
    None — «нажатия есть, но мы их не видим». Раньше и то и другое было
    пустым списком, и вызывающий не мог ни отступить, ни пожаловаться:
    он молотил отказывающий адрес по разу в секунду, заливая лог.
    """
    if not token:
        return None
    try:
        response = _post(
            UPDATES_API.format(token=token),
            json={"offset": offset, "timeout": timeout,
                  # channel_post нужен, чтобы бот сам назвал id канала:
                  # искать его вручную — возня, а бот и так админ и видит
                  # каждую публикацию.
                  "allowed_updates": ["callback_query", "message",
                                      "channel_post"]},
            timeout=timeout + 10.0)
        body = response.json()
    except httpx.HTTPError as exc:
        print(f"[telegram] getUpdates: сеть — {exc}")
        return None
    except ValueError:
        print("[telegram] getUpdates: ответ не разобрался")
        return None

    if not body.get("ok"):
        # 409 означает установленный вебхук: пока он есть, getUpdates
        # не отдаст ни одного обновления, и кнопки мертвы полностью.
        print(f"[telegram] getUpdates: {body.get('error_code')} "
              f"{body.get('description')}")
        return None
    return body.get("result", [])


def webhook_info(token: str) -> dict:
    """Что Телеграм думает о вебхуке. Пустой url — можно опрашивать."""
    if not token:
        return {}
    return _call(WEBHOOK_INFO_API.format(token=token), {}) or {}


def bot_identity(token: str) -> dict:
    """Кто мы для Телеграма. Заодно проверка, что токен вообще живой."""
    if not token:
        return {}
    return _call(ME_API.format(token=token), {}) or {}
