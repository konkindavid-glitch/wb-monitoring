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
from monitoring.delivery import format_digest, format_urgent, send
from monitoring.factors.judgment import judgment_factors
from monitoring.factors.mechanical import mechanical_factors
from monitoring.heartbeat import build_report, map_hits_to_questions
from monitoring.scoring import score_item
from monitoring.topics import build_matchers, classify, detect_platform

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
    store: object = None
    sources: list = None
    sender: object = send
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
        if deps.sender(format_urgent(hit), deps.token, deps.chat_id):
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


# --- запуск ---------------------------------------------------------------

DB_DEFAULTS = {
    "host": "amvera-davidkonkin-cnpg-monitoring-db-rw",
    "port": "5432",
    "user": "monitoring",
    "dbname": "monitoring",
}


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
        token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )

    if not dry_run:
        from monitoring.db import Repo, apply_migration, open_connection
        kind, source = database_source()
        try:
            conn = (open_connection(source) if kind == "url"
                    else open_connection(**source))
        except Exception as exc:
            fallback = database_parts()
            if kind == "url" and fallback:
                # DATABASE_URL не разобрался — почти всегда это спецсимвол
                # в пароле, который в URL обязан быть percent-кодирован.
                # Раз отдельные параметры заданы, переключаемся на них
                # вместо падения: экранировать там нечего.
                print(f"[warn] DATABASE_URL не сработал ({exc}); "
                      "перехожу на DB_PASSWORD")
                conn = open_connection(**fallback)
            elif kind == "url":
                raise RuntimeError(
                    f"не удалось подключиться по DATABASE_URL: {exc}\n"
                    "Если в пароле есть @ : / ? # или %, в URL их нужно "
                    "percent-кодировать. Проще задать DB_PASSWORD отдельной "
                    "переменной — там экранировать ничего не надо."
                ) from exc
            else:
                raise
        apply_migration(conn, ROOT / "sql" / "001_monitoring_map.sql")
        deps.repo = Repo(conn)

    from monitoring.factors.judgment import build_client
    deps.judge = build_client()

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
    args = parser.parse_args()

    cfg = load_config(ROOT)

    if args.onboard:
        run_onboarding(cfg)
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
          f"классификатор: {'есть' if deps.judge else 'НЕТ'}")

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
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
