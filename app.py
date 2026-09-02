#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Сборщик «Карты мониторинга».

Режимы:
    python app.py            постоянный цикл (контейнер в Амвере)
    python app.py --once     один проход (Cron Jobs)
    python app.py --dry-run  сбор без записи в базу и без отправки

Выбор между циклом и Cron Jobs откладывается до первых замеров: тик короткий,
и платить за круглосуточный контейнер может оказаться незачем. Разница между
режимами — один флаг.
"""
import argparse
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

# line_buffering обязателен: в контейнере stdout идёт не в терминал, а в канал,
# и Python копит вывод блоками по несколько килобайт. Логи тогда появляются
# с большой задержкой или только при падении — аварийное завершение сбрасывает
# буфер. Для мониторинга это худший из вариантов: работающий сервис выглядит
# мёртвым, а видно только то, что упало.
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

from monitoring import stop_rules
from monitoring.collectors import doc_diff, rss
from monitoring.config import load_config
from monitoring.confirmation import count_independent_sources, repeats_of
from monitoring.delivery import (answer_callback, delete_message,
                                 format_digest, get_updates,
                                 replace_text, send, send_card)
from monitoring.moderation import (EDIT, EDIT_PROMPT, OUTCOME_TOAST,
                                   PUBLISH, REJECT, keyboard,
                                   outcome_text, parse_callback)
from monitoring.factors.judgment import judgment_factors
from monitoring.factors.mechanical import mechanical_factors
from monitoring.health import format_status
from monitoring.heartbeat import build_report, map_hits_to_questions
from monitoring.post import format_card, sample_card
from monitoring.scoring import score_item
from monitoring.topics import build_matchers, classify, detect_platform
from monitoring.writer import article_text, write_post

ROOT = Path(__file__).resolve().parent
POLL_SECONDS = 60
DIGEST_EVERY_HOURS = 12
BATCH_SIZE = 15
CONFIRMATION_WINDOW_HOURS = 72

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


@dataclass
class Deps:
    cfg: object
    fetcher: object = None
    repo: object = None
    judge: object = None
    writer: object = None
    store: object = None
    sources: list = None
    sender: object = send
    dry_run: bool = False
    token: str = ""
    chat_id: str = ""


# --- расписание -----------------------------------------------------------

def due_classes(last_run: dict, now: datetime, cadence_cfg: dict) -> list:
    """Какие классы частоты созрели к опросу."""
    due = []
    for key in sorted(cadence_cfg):
        last = last_run.get(key)
        if last is None or (now - last).total_seconds() >= cadence_cfg[key]:
            due.append(key)
    return due


def acquire_tick_lock(cadence_class: str) -> bool:
    """Два тика одного класса одновременно дадут дубли и двойной счёт."""
    with _LOCKS_GUARD:
        if _LOCKS.get(cadence_class):
            return False
        _LOCKS[cadence_class] = True
        return True


def release_tick_lock(cadence_class: str) -> None:
    with _LOCKS_GUARD:
        _LOCKS[cadence_class] = False


# --- тик ------------------------------------------------------------------

def run_tick(cadence_class: str, deps: Deps, now: datetime) -> dict:
    counters = {"fetched": 0, "stopped": 0, "scored": 0,
                "urgent": 0, "model_calls": 0}
    run_id = deps.repo.start_run(cadence_class)

    collected = []
    for source in (deps.sources or []):
        if source.get("cadence") != cadence_class:
            continue
        if source["method"] == "rss":
            collected += rss.collect(source, deps.fetcher, now)
        elif source["method"] == "doc_diff":
            collected += doc_diff.collect(source, deps.fetcher, deps.store, now)
    counters["fetched"] = len(collected)

    matchers = build_matchers(deps.cfg)

    # Разметка идёт до стоп-правил: она дешёвая и детерминированная, а правилу
    # по длине нужно знать, попал ли материал в тему. Иначе заголовок без тела
    # «В Ozon уточнили условия постоплаты» отсеивается как «слишком общий».
    # На подсчёт баллов порядок не влияет — стоп-правила по-прежнему раньше.
    survivors = []
    for item in collected:
        if deps.repo.is_known(item.url_hash):
            continue

        topics, categories = classify(item, matchers, deps.cfg)
        item = replace(item, topics=topics, categories=categories)

        # Площадка по содержанию важнее площадки источника: статья про WB на
        # отраслевом сайте иначе теряет +25 — самый весомый фактор матрицы.
        if item.platform == "CROSS_PLATFORM":
            detected = detect_platform(item, matchers, deps.cfg)
            if detected:
                item = replace(item, platform=detected)

        verdict = stop_rules.check(item, deps.cfg)
        if verdict.stopped:
            deps.repo.record_stop(item, verdict, run_id)
            counters["stopped"] += 1
            continue
        survivors.append(item)

    judged = {}
    stats = {}
    if survivors and deps.judge is not None:
        judged = judgment_factors(survivors, deps.judge,
                                  batch_size=BATCH_SIZE, stats=stats)
        counters["model_calls"] = stats.get("batches", 0) - stats.get("failed", 0)
        counters["model_failed"] = stats.get("failed", 0)
        if stats.get("failed"):
            # Без факторов-суждений максимум — 65 из 160, и всё уходит в DROP.
            # Снаружи это выглядит как «новостей нет», поэтому кричим.
            print(f"[degraded] классификатор недоступен: {stats.get('error')}")
    elif survivors and deps.judge is None:
        counters["model_failed"] = -1
        print("[degraded] нет ни OPENROUTER_API_KEY, ни ANTHROPIC_API_KEY — "
              "7 факторов из 14 не проставляются, оценки систематически занижены")

    weights = deps.cfg.factor_weights()
    thresholds = deps.cfg.thresholds()

    # Пул для подсчёта подтверждений — текущий тик плюс окно недавних находок
    # из базы. Только по тику считать нельзя: новость, вышедшая на одном сайте
    # в 10:00, а на другом в 14:00, попадёт в разные тики и не встретится.
    pool = list(survivors)
    if hasattr(deps.repo, "recent_for_confirmation"):
        try:
            pool += deps.repo.recent_for_confirmation(CONFIRMATION_WINDOW_HOURS)
        except Exception as exc:
            print(f"[warn] окно подтверждений недоступно: {exc}")

    scored_hits = []
    for item in survivors:
        # Одна и та же новость из трёх изданий — три независимых подтверждения,
        # и штраф −50 снимается. Без этого он срабатывал на всём, что пришло
        # не из официального источника, и подъём из BACKLOG был мёртв.
        sources = count_independent_sources(item, pool)
        known = {item.url_hash} if repeats_of(item, pool) else set()

        fired = mechanical_factors(item, deps.cfg, known_urls=known,
                                   independent_sources=sources)
        fired.update(judged.get(item.url_hash, {}))

        result = score_item(fired, weights, thresholds)
        hit_id = deps.repo.save_hit(item, result, run_id)
        scored_hits.append((hit_id, item, result))
        counters["scored"] += 1
        if result.decision == "URGENT":
            counters["urgent"] += 1

    counters["hits"] = scored_hits

    status = "DEGRADED" if counters.get("model_failed") else "SUCCESS"
    deps.repo.finish_run(run_id, status=status, fetched=counters["fetched"],
                         stopped=counters["stopped"], scored=counters["scored"],
                         model_calls=counters["model_calls"])
    return counters


# --- heartbeat и доставка -------------------------------------------------

def run_heartbeat(deps: Deps, state: dict, now: datetime) -> dict:
    weights = deps.cfg.factor_weights()
    thresholds = deps.cfg.thresholds()
    deps.repo.promote_backlog(now, weights, thresholds)

    urgent = deps.repo.pending_urgent()
    queued = deps.repo.pending_digest()
    degraded = deps.repo.degraded_sources()

    report = build_report({
        "last_run_at": state.get("last_run_at", {}),
        "hits_by_question": state.get("hits_by_question", {}),
        "urgent_count": len(urgent),
        "queue_count": len(queued),
        "model_calls": state.get("model_calls", 0),
    }, now)

    deps.repo.save_heartbeat(report, state.get("run_id"))

    delivered = []
    failed_sends = 0
    for hit in urgent:
        if deliver_card(hit, deps):
            delivered.append(hit["hit_id"])
        else:
            failed_sends += 1

    last_digest = state.get("last_digest_at")
    digest_due = (last_digest is None or
                  (now - last_digest).total_seconds() >= DIGEST_EVERY_HOURS * 3600)
    # Пустой тик не отправляется: тридцать сообщений «нового нет» подряд
    # приучают не открывать бота вернее, чем отсутствие мониторинга.
    if digest_due and (queued or degraded):
        if deps.sender(format_digest(queued, degraded, report),
                       deps.token, deps.chat_id):
            delivered += [hit["hit_id"] for hit in queued]
            state["last_digest_at"] = now
            print(f"[delivery] дайджест отправлен: {len(queued)} материалов, "
                  f"источников с проблемами: {len(degraded)}")
        else:
            failed_sends += 1

    # Без этой строки непонятно, дошло ли что-то до бота: очередь может быть
    # пустой по делу, а может — из-за молчащего Телеграма, и снаружи это
    # выглядит одинаково.
    if urgent or failed_sends:
        print(f"[delivery] срочных: {len(urgent)}, отправлено: {len(delivered)}, "
              f"не ушло: {failed_sends}")
    if failed_sends:
        print("[degraded] Телеграм не принимает сообщения — "
              "проверьте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")

    deps.repo.mark_delivered(delivered)
    return report


def render_cover(hit: dict):
    """Обложка или None. Ни отсутствие шрифта, ни отсутствие Pillow
    не должны отменять доставку находки — текст важнее картинки."""
    try:
        from monitoring.cover import render
        return render(hit)
    except ImportError:
        print("[degraded] Pillow не установлен — карточки уходят без обложки")
    except Exception as exc:
        print(f"[degraded] обложка не отрисовалась: {exc}")
    return None


def deliver_card(hit: dict, deps, is_test: bool = False) -> bool:
    """Карточка редактору: обложка, разбор, три кнопки решения."""
    if deps.dry_run:
        print(format_card(hit, is_test=is_test))
        return True
    message_id = send_card(
        format_card(hit, is_test=is_test), deps.token, deps.chat_id,
        cover=render_cover(hit),
        reply_markup=keyboard(hit.get("hit_id", "hit_sample")))
    return message_id is not None


# --- решения редактора -----------------------------------------------------

# Состояние находки после каждого решения. «Запостить» переводит в HANDED_OFF:
# находка согласована и отдана наружу. Публикацией займётся слой публикации —
# текстов постов этот модуль не пишет, см. README «Границы модуля».
STATE_AFTER = {PUBLISH: "HANDED_OFF", REJECT: "DROPPED"}

# Смещение getUpdates хранится на диске: без него после перезапуска Телеграм
# отдаёт сутки накопленных нажатий заново, и редактор получает шквал уже
# принятых решений.
def offset_path() -> Path:
    snapshots = Path(os.getenv("SNAPSHOT_DIR", str(ROOT / "data" / "snapshots")))
    return snapshots.parent / "telegram-offset"


def read_offset() -> int:
    try:
        return int(offset_path().read_text().strip())
    except (OSError, ValueError):
        return 0


def write_offset(value: int) -> None:
    try:
        path = offset_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value))
    except OSError as exc:
        print(f"[degraded] смещение обновлений не сохранено: {exc}")


def resolve_hit(hit_id: str, deps):
    """Находка по идентификатору. Эталонной в базе нет — она задана в коде."""
    if hit_id == "hit_sample":
        return sample_card()
    if deps.repo is None:
        return None
    try:
        return deps.repo.hit_by_id(hit_id)
    except Exception as exc:
        print(f"[fail] находка {hit_id} не прочитана: {exc}")
        if hasattr(deps.repo, "rollback"):
            deps.repo.rollback()
        return None


def publish_hit(hit_id: str, deps):
    """Пишет пост по находке и публикует. Возвращает (получилось, что сказать).

    Пост уходит в канал из TELEGRAM_CHANNEL_ID, а если канал не задан —
    в тот же чат модерации. Молчаливое «никуда» недопустимо: редактор нажал
    кнопку и обязан увидеть результат.
    """
    hit = resolve_hit(hit_id, deps)
    if hit is None:
        return False, "находка не найдена в базе"

    url = hit.get("url") or ""
    source = article_text(url, deps.fetcher) if url else ""
    written = write_post(hit, source, deps.writer or deps.judge)
    if not written:
        return False, written.reason

    # Ссылка дописывается кодом, а не моделью: единственное место, где её
    # нельзя переврать.
    text = f"{written.text}\n\n{url}" if url else written.text

    channel = os.getenv("TELEGRAM_CHANNEL_ID", "")
    target = channel or deps.chat_id
    message_id = send_card(text, deps.token, target, cover=render_cover(hit))
    if message_id is None:
        return False, f"Телеграм не принял пост в {target}"
    if channel:
        return True, "✅ Опубликовано в канале"
    return True, ("✅ Пост готов и отправлен сюда — канал не задан, "
                  "укажите TELEGRAM_CHANNEL_ID для публикации")


def handle_callback(query: dict, deps) -> None:
    """Нажатие кнопки под карточкой."""
    parsed = parse_callback(query.get("data", ""))
    if parsed is None:
        return
    action, hit_id = parsed
    _PRESSES["count"] += 1
    message = query.get("message") or {}
    message_id = message.get("message_id")
    chat_id = str((message.get("chat") or {}).get("id", deps.chat_id))
    editor = (query.get("from") or {}).get("id")

    # Часики на кнопке гасятся первым делом: всё остальное может занять
    # секунды, а редактор всё это время смотрит на крутилку.
    answer_callback(query.get("id", ""), deps.token, OUTCOME_TOAST.get(action))

    # Карточка берётся из самого сообщения: перечитывать базу незачем,
    # а для эталонной карточки её там и нет.
    card = message.get("caption") or message.get("text") or ""
    has_caption = "caption" in message

    prompt_message_id = None
    if action == EDIT:
        prompt_message_id = send_prompt(chat_id, deps)

    record_decision(hit_id, action, chat_id, message_id, prompt_message_id,
                    editor, deps)

    if action == REJECT:
        set_state(hit_id, STATE_AFTER[REJECT], deps)
        delete_message(message_id, deps.token, chat_id)
        print(f"[moderation] {action} · {hit_id}")
        return

    if action == EDIT:
        replace_text(message_id, outcome_text(action, card), deps.token,
                     chat_id, has_caption=has_caption)
        print(f"[moderation] {action} · {hit_id}")
        return

    # Написание поста занимает секунды: сначала убираются кнопки, чтобы
    # второе нажатие не опубликовало пост дважды, и показывается, что идёт
    # работа, — иначе нажатие выглядит как «ничего не произошло».
    replace_text(message_id, outcome_text(action, card, "⏳ Пишу пост…"),
                 deps.token, chat_id, has_caption=has_caption)

    ok, note = publish_hit(hit_id, deps)
    if ok:
        set_state(hit_id, STATE_AFTER[PUBLISH], deps)
        replace_text(message_id, outcome_text(action, card, note),
                     deps.token, chat_id, has_caption=has_caption)
    else:
        # Кнопки возвращаются: причина сбоя устранима — источник ответит,
        # ключ пополнится, — и попытку надо дать повторить.
        replace_text(message_id,
                     outcome_text(action, card, f"⚠️ Пост не написан: {note}"),
                     deps.token, chat_id, has_caption=has_caption,
                     reply_markup=keyboard(hit_id))
    print(f"[moderation] {action} · {hit_id} · {note}")


def record_decision(hit_id, action, chat_id, message_id, prompt_message_id,
                    editor, deps) -> None:
    if deps.repo is None:
        return
    try:
        deps.repo.save_decision(hit_id, action, chat_id, message_id,
                                prompt_message_id, editor)
    except Exception as exc:
        print(f"[fail] решение не записано: {exc}")
        if hasattr(deps.repo, "rollback"):
            deps.repo.rollback()


def set_state(hit_id: str, state: str, deps) -> None:
    if deps.repo is None:
        return
    try:
        deps.repo.set_hit_state(hit_id, state)
    except Exception as exc:
        print(f"[fail] состояние находки не обновлено: {exc}")
        if hasattr(deps.repo, "rollback"):
            deps.repo.rollback()


def send_prompt(chat_id: str, deps):
    """Приглашение прислать правку ответом. Возвращает message_id или None."""
    from monitoring.delivery import API, _call
    result = _call(API.format(token=deps.token),
                   {"chat_id": chat_id, "text": EDIT_PROMPT,
                    "reply_markup": json.dumps({"force_reply": True})})
    return result.get("message_id") if result else None


def handle_message(message: dict, deps) -> None:
    """Ответ редактора на приглашение к правке."""
    text = (message.get("text") or "").strip()
    chat_id = str((message.get("chat") or {}).get("id", deps.chat_id))

    if text.lower().lstrip("/").split("@")[0] in STATUS_COMMANDS:
        send(format_status(collect_status(deps)), deps.token, chat_id)
        print("[moderation] запрошено состояние")
        return

    reply_to = (message.get("reply_to_message") or {}).get("message_id")
    if not reply_to or not text:
        return
    try:
        found = deps.repo.hit_id_awaiting_note(reply_to)
        if not found:
            return
        decision_id, hit_id = found
        deps.repo.save_note(decision_id, text)
    except Exception as exc:
        print(f"[fail] правка не записана: {exc}")
        if hasattr(deps.repo, "rollback"):
            deps.repo.rollback()
        return
    send(f"Правка записана к находке {hit_id}.", deps.token, chat_id)
    print(f"[moderation] правка · {hit_id}")


# --- состояние по запросу ---------------------------------------------------

# Отметка сборки. Если бот отвечает на команду и показывает эту строку,
# значит новый код доехал и работает — а это первое, что нужно знать,
# когда «ничего не сработало».
BUILD = "2026-09-02 · публикация постов"

STATUS_COMMANDS = {"статус", "status", "диагностика", "ping"}

# Счётчик обработанных нажатий: отличает «кнопку не нажимали» от
# «нажатие пришло, но результат не дошёл».
_PRESSES = {"count": 0}
_LAST_TICK = {"text": ""}


def check_telegram(deps) -> None:
    """Проверка связи при старте: токен и вебхук.

    Вебхук проверяется отдельной строкой не для полноты. Пока он установлен,
    getUpdates отвечает 409 и не отдаёт ни одного обновления — кнопки мертвы
    целиком, и выглядит это ровно как «никто ничего не нажимал». Такое
    состояние обязано быть громким, а не выясняться перебором догадок.
    """
    from monitoring.delivery import bot_identity, webhook_info
    from monitoring.netcheck import report

    # Факты о сети печатаются до попытки достучаться: три предыдущих объяснения
    # отказа были догадками, и каждая стоила пересборки.
    try:
        for line in report():
            print(line)
    except Exception as exc:
        print(f"[net] проверка сети не удалась: {exc}")

    if not deps.token:
        print("[degraded] нет TELEGRAM_BOT_TOKEN — кнопки работать не будут")
        return

    identity = bot_identity(deps.token)
    if not identity:
        print("[degraded] Телеграм не признал TELEGRAM_BOT_TOKEN")
        return
    print(f"[start] бот @{identity.get('username', '?')}")

    url = webhook_info(deps.token).get("url", "")
    if not url:
        return
    print(f"[degraded] у бота установлен вебхук ({url}): getUpdates не отдаёт "
          f"обновления, кнопки не работают. Вебхук нужно снять.")
    send("\n".join([
        "⚠️ Кнопки не будут работать.",
        "",
        f"У бота установлен вебхук: {url}",
        "Пока он есть, Телеграм отдаёт нажатия ему, а не сюда.",
        "",
        "Снять его можно, открыв в браузере:",
        f"https://api.telegram.org/bot<ТОКЕН>/deleteWebhook",
    ]), deps.token, deps.chat_id)


def collect_status(deps) -> dict:
    """Факты о состоянии. Каждый добывается отдельно: недоступная база
    не должна помешать узнать про вебхук, и наоборот."""
    from monitoring.delivery import bot_identity, webhook_info

    facts = {"build": BUILD, "channel": os.getenv("TELEGRAM_CHANNEL_ID", ""),
             "last_tick": _LAST_TICK["text"], "presses": _PRESSES["count"]}

    facts["bot_username"] = bot_identity(deps.token).get("username", "")
    facts["webhook_url"] = webhook_info(deps.token).get("url", "")

    if deps.repo is None:
        facts["db_error"] = "не подключена"
    else:
        try:
            deps.repo.degraded_sources()
            facts["db_error"] = ""
        except Exception as exc:
            facts["db_error"] = str(exc)[:200]
            if hasattr(deps.repo, "rollback"):
                deps.repo.rollback()

    client = deps.writer or deps.judge
    facts["model"] = getattr(client, "_model", "") if client else ""
    return facts


# Каналы, о которых уже сказали. Повторять при каждой публикации незачем:
# одно сообщение полезно, десять — шум, из-за которого перестают читать бота.
_ANNOUNCED_CHATS = set()


def channel_is_configured(chat: dict) -> bool:
    configured = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
    if not configured:
        return False
    username = chat.get("username") or ""
    return configured in {str(chat.get("id")), f"@{username}", username}


def handle_channel_post(post: dict, deps) -> None:
    """Бот увидел публикацию в канале — значит знает его id, и говорит его.

    Узнавать id канала вручную неудобно: у приватного канала нет имени,
    а гонять пользователя к сторонним ботам за идентификатором его же
    канала — плохой совет. Бот админ, он видит публикации, и сказать id
    может сам.
    """
    chat = post.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None or chat.get("type") != "channel":
        return
    if channel_is_configured(chat) or chat_id in _ANNOUNCED_CHATS:
        return
    _ANNOUNCED_CHATS.add(chat_id)

    title = chat.get("title") or "без названия"
    username = chat.get("username")
    value = f"@{username}" if username else str(chat_id)
    send("\n".join([
        f"Нашёл канал «{title}».",
        "",
        "Чтобы посты уходили туда, задайте в Амвере переменную:",
        f"TELEGRAM_CHANNEL_ID={value}",
        "",
        "После сохранения Амвера перезапустит контейнер сама.",
    ]), deps.token, deps.chat_id)
    print(f"[chat] канал «{title}» id={chat_id} username={username or '—'}")


def poll_moderation(deps, seconds: int) -> None:
    """Слушает нажатия отведённое время, потом возвращает управление циклу.

    Длинный опрос вместо коротких запросов: соединение держит сервер Телеграма,
    трафика почти нет, а реакция на кнопку приходит сразу, а не через минуту.
    """
    if deps.dry_run or not deps.token:
        time.sleep(seconds)
        return

    deadline = time.monotonic() + seconds
    offset = read_offset()
    backoff = 1.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        updates = get_updates(deps.token, offset, timeout=int(min(25, remaining)))

        # None — отказ, а не отсутствие нажатий. Отказ бывает мгновенным
        # (ENETUNREACH возвращается сразу, без попытки соединиться), и без
        # паузы цикл повторял запрос по разу в секунду, заливая лог.
        if updates is None:
            time.sleep(min(backoff, max(remaining, 0)))
            backoff = min(backoff * 2, 30.0)
            continue
        backoff = 1.0

        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "callback_query" in update:
                    handle_callback(update["callback_query"], deps)
                elif "message" in update:
                    handle_message(update["message"], deps)
                elif "channel_post" in update:
                    handle_channel_post(update["channel_post"], deps)
            except Exception as exc:
                print(f"[fail] обновление {update.get('update_id')}: {exc}")
        if updates:
            write_offset(offset)


# --- запуск ---------------------------------------------------------------

DB_DEFAULTS = {
    "host": "amvera-davidkonkin-cnpg-monitoring-db-rw",
    "port": "5432",
    "user": "monitoring",
    "dbname": "monitoring",
}


def connect_database():
    """Соединение с базой по любому из двух путей.

    Одна точка на весь модуль намеренно: откат с DATABASE_URL на DB_PASSWORD
    уже однажды разошёлся между build_deps и send_test_post, и проверочная
    карточка ушла эталонной вместо настоящей находки.
    """
    from monitoring.db import open_connection

    kind, source = database_source()
    if kind is None:
        raise RuntimeError("не задан ни DATABASE_URL, ни DB_PASSWORD")
    try:
        return open_connection(source) if kind == "url"             else open_connection(**source)
    except Exception as exc:
        fallback = database_parts()
        if kind == "url" and fallback:
            print(f"[warn] DATABASE_URL не сработал ({exc}); перехожу на DB_PASSWORD")
            return open_connection(**fallback)
        if kind == "url":
            raise RuntimeError(
                f"не удалось подключиться по DATABASE_URL: {exc}\n"
                "Если в пароле есть @ : / ? # или %, в URL их нужно "
                "percent-кодировать. Проще задать DB_PASSWORD отдельной "
                "переменной — там экранировать ничего не надо."
            ) from exc
        raise


def database_parts():
    """Параметры подключения из отдельных переменных или None.

    Экранирования не требуют вовсе — в этом и смысл второго пути.
    """
    password = os.getenv("DB_PASSWORD")
    if not password:
        return None
    parts = dict(DB_DEFAULTS, password=password)
    for env_name, key in (("DB_HOST", "host"), ("DB_PORT", "port"),
                          ("DB_USER", "user"), ("DB_NAME", "dbname")):
        if os.getenv(env_name):
            parts[key] = os.getenv(env_name)
    return parts


def database_source() -> tuple:
    """Как подключаться к базе: ('url', dsn) либо ('parts', {...}), либо (None, None).

    DATABASE_URL удобен одной строкой, но пароль в нём обязан быть
    percent-кодирован: один символ @ или # роняет разбор ещё до подключения,
    и ошибка выглядит как сбой парсера, а не как «поправьте пароль».
    Поэтому DB_PASSWORD — равноправный путь, где экранировать не нужно ничего.
    """
    url = os.getenv("DATABASE_URL")
    if url:
        return "url", url
    parts = database_parts()
    if parts:
        return "parts", parts
    return None, None


def check_environment(dry_run: bool) -> list:
    """Чего не хватает для работы. Пустой список — всё на месте."""
    missing = []
    if not dry_run and database_source()[0] is None:
        missing.append("DATABASE_URL или DB_PASSWORD — "
                       "без базы находки некуда сохранять")
    if not os.getenv("OPENROUTER_API_KEY") and not os.getenv("ANTHROPIC_API_KEY"):
        missing.append("OPENROUTER_API_KEY — без него 7 факторов из 14 "
                       "не проставляются и всё уходит в DROP")
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        missing.append("TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID — "
                       "находки будут копиться в базе, но не приходить в бота")
    return missing


def wait_for_configuration(missing: list) -> None:
    """Ждёт настройки вместо падения.

    Аварийное завершение здесь дало бы цикл перезапусков: контейнер падает,
    Амвера поднимает его снова, лог заполняется одинаковыми трейсбеками, и за
    это ещё капают деньги. Спокойное ожидание оставляет лог читаемым, а после
    добавления переменных Амвера перезапустит контейнер сама.
    """
    print("=" * 70)
    print("СЕРВИС НЕ НАСТРОЕН. Не хватает переменных окружения:")
    for line in missing:
        print(f"  • {line}")
    print()
    print("Задать их: проект wb-monitoring → вкладка «Переменные».")
    print("После сохранения Амвера перезапустит контейнер сама.")
    print("=" * 70)
    while True:
        time.sleep(300)
        print("[waiting] переменные окружения всё ещё не заданы")


def build_deps(cfg, dry_run: bool) -> Deps:
    from monitoring.collectors.base import Fetcher
    from monitoring.collectors.doc_diff import SnapshotStore

    snapshot_dir = os.getenv("SNAPSHOT_DIR", str(ROOT / "data" / "snapshots"))
    deps = Deps(
        cfg=cfg,
        fetcher=Fetcher(),
        store=SnapshotStore(snapshot_dir),
        sources=cfg.source_list(),
        dry_run=dry_run,
        token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )

    if not dry_run:
        from monitoring.db import Repo, apply_migration
        conn = connect_database()
        apply_migration(conn, ROOT / "sql" / "001_monitoring_map.sql")
        deps.repo = Repo(conn)

    from monitoring.factors.judgment import build_client
    deps.judge = build_client()
    deps.writer = build_client("WRITER_MODEL")

    return deps


class DryRunRepo:
    """Показывает, что нашлось, ничего не записывая."""

    def __init__(self):
        self.seen = set()

    def start_run(self, cadence):
        return "run_dry"

    def finish_run(self, run_id, **kw):
        pass

    def is_known(self, url_hash):
        return url_hash in self.seen

    def record_stop(self, item, verdict, run_id):
        print(f"  [стоп] {verdict.code}: {item.title[:70]}")

    def save_hit(self, item, result, run_id):
        self.seen.add(item.url_hash)
        print(f"  [{result.decision}] {result.score:>4} — {item.title[:70]}")
        print(f"          темы: {', '.join(item.topics) or '—'}")
        return "hit_dry"

    def recent_for_confirmation(self, hours=72):
        return []

    def promote_backlog(self, now, weights, thresholds):
        return []

    def pending_urgent(self):
        return []

    def pending_digest(self):
        return []

    def degraded_sources(self):
        return []

    def save_heartbeat(self, report, run_id=None):
        pass

    def mark_delivered(self, hit_ids):
        pass


def send_test_post(cfg, dry_run: bool = False) -> bool:
    """Отправляет карточку в Телеграм: проверка, что связь работает.

    Берёт лучшую находку из базы; если база пуста или недоступна — эталонную
    из docs/01 §2.3. Выдумывать содержимое нельзя даже в проверочном
    сообщении, поэтому эталон честно помечен как образец.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not dry_run and (not token or not chat_id):
        print("[test-post] нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return False

    hit = None
    try:
        from monitoring.db import Repo
        conn = connect_database()
        best = Repo(conn)._pending(("URGENT", "QUEUE", "BACKLOG", "DROP"), 1)
        hit = best[0] if best else None
        conn.close()
    except Exception as exc:
        print(f"[test-post] база недоступна ({exc}), беру эталонную карточку")

    if hit is None:
        hit = sample_card()
        print("[test-post] в базе нет находок — отправляю эталонную карточку")
    else:
        print(f"[test-post] беру находку из базы: {hit.get('score')} баллов")

    probe = Deps(cfg=cfg, dry_run=dry_run, token=token, chat_id=chat_id)
    ok = deliver_card(hit, probe, is_test=True)
    print("[test-post] отправлено" if ok else "[test-post] Телеграм не принял")
    return ok


def run_onboarding(cfg) -> list:
    """Проверяет каждый источник и печатает вердикт.

    Адреса лент меняются, часть площадок закрывает RSS без предупреждения.
    Источник, не прошедший проверку, должен быть виден, а не молча отдавать
    ноль элементов месяцами.
    """
    from monitoring.collectors.base import Fetcher
    from monitoring.collectors.onboarding import validate_source

    fetcher = Fetcher()
    now = datetime.now(timezone.utc)
    onboarding_cfg = cfg.onboarding_cfg()
    failed = []

    print(f"{'источник':<24} {'метод':<9} вердикт")
    print("-" * 78)
    for source in cfg.source_list():
        report = validate_source(source, fetcher, now, onboarding_cfg)
        mark = "OK  " if report.ok else "FAIL"
        detail = report.reason or f"элементов: {report.checks.get('items', '—')}"
        print(f"{source['key']:<24} {source['method']:<9} {mark}  {detail}")
        if not report.ok:
            failed.append(source["key"])

    print("-" * 78)
    print(f"прошли: {len(cfg.source_list()) - len(failed)} из {len(cfg.source_list())}")
    if failed:
        print("требуют внимания: " + ", ".join(failed))
    return failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument("--dry-run", action="store_true",
                        help="без записи в базу и без отправки")
    parser.add_argument("--onboard", action="store_true",
                        help="проверить источники и выйти")
    parser.add_argument("--test-post", action="store_true",
                        help="отправить карточку в Телеграм и выйти")
    args = parser.parse_args()

    cfg = load_config(ROOT)

    if args.onboard:
        run_onboarding(cfg)
        return

    # Переменной окружения тоже можно: в Амвере запустить команду с флагом
    # негде, а выставить TEST_POST=1 и перезапустить контейнер — можно.
    if args.test_post or os.getenv("TEST_POST"):
        send_test_post(cfg, dry_run=args.dry_run)
        if args.test_post:
            return

    # Проверка до подключений: без DATABASE_URL сервис падал бы на первой
    # строке и Амвера крутила бы его в цикле перезапусков.
    missing = check_environment(args.dry_run)
    critical = [m for m in missing if m.startswith("DATABASE_URL")]
    if critical:
        wait_for_configuration(missing)
    for line in missing:
        print(f"[degraded] не задано: {line}")
    cadence = cfg.cadence_seconds()
    deps = build_deps(cfg, args.dry_run)
    if args.dry_run:
        deps.repo = DryRunRepo()
        deps.sender = lambda text, token, chat: (print(text), True)[1]

    state = {"last_run_at": {}, "hits_by_question": {}}

    # Отметка старта: по ней видно, что сервис жив, ещё до первого тика.
    # Первый полный проход опрашивает все шесть классов и может занять
    # несколько минут — без этой строки он выглядит как зависание.
    print(f"[start] сборщик запущен, классов: {len(cadence)}, "
          f"источников: {len(deps.sources or [])}, "
          f"классификатор: {'есть' if deps.judge else 'НЕТ'}, "
          f"сборка: {BUILD}")
    check_telegram(deps)

    while True:
        now = datetime.now(timezone.utc)
        for cadence_class in due_classes(state["last_run_at"], now, cadence):
            if not acquire_tick_lock(cadence_class):
                print(f"[skip] class={cadence_class} SKIPPED_OVERLAP")
                continue
            try:
                counters = run_tick(cadence_class, deps, now)
                hits = counters.pop("hits", [])
                state["hits_by_question"].update(map_hits_to_questions(hits))
                print(f"[tick] class={cadence_class} {counters}")
                _LAST_TICK["text"] = f"класс {cadence_class}, {counters}"
            except Exception as exc:
                print(f"[fail] class={cadence_class}: {exc}")
                # Откат обязателен: Postgres переводит транзакцию в aborted,
                # и без него каждая следующая команда отвечает «current
                # transaction is aborted» — сыплются все оставшиеся классы.
                if hasattr(deps.repo, "rollback"):
                    deps.repo.rollback()
            finally:
                release_tick_lock(cadence_class)
            state["last_run_at"][cadence_class] = now

        try:
            run_heartbeat(deps, state, now)
        except Exception as exc:
            print(f"[fail] heartbeat: {exc}")
            if hasattr(deps.repo, "rollback"):
                deps.repo.rollback()

        if args.once:
            break
        poll_moderation(deps, POLL_SECONDS)


if __name__ == "__main__":
    main()
