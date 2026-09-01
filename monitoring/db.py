"""Доступ к Postgres: соединение, идемпотентная миграция, репозитории."""
import io
import json
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

import psycopg


@contextmanager
def connect(dsn: str = None, **parts):
    """Соединение по строке DATABASE_URL или по отдельным параметрам.

    Второй путь существует не для красоты: в URL пароль обязан быть
    percent-кодирован, и один символ @ или # в нём роняет разбор строки
    ещё до попытки подключиться. Ошибка при этом выглядит как невнятный
    сбой парсера, а не как «поправьте пароль». Отдельные параметры
    экранирования не требуют вовсе.
    """
    conn = psycopg.connect(dsn, autocommit=False) if dsn \
        else psycopg.connect(autocommit=False, **parts)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_migration(conn, sql_path: Path) -> None:
    """Применяет миграцию. Безопасна при повторном вызове — сервис перезапускается.

    Файл содержит собственные BEGIN и COMMIT: он рассчитан и на запуск через
    `psql -f`. Внутри соединения с autocommit=False psycopg открывает свою
    транзакцию первой, и тогда BEGIN из файла ругается «транзакция уже идёт»,
    а COMMIT закрывает чужую. Поэтому на время миграции отдаём управление
    транзакциями самому файлу.
    """
    with io.open(sql_path, encoding="utf-8") as fh:
        sql = fh.read()

    previous = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.autocommit = previous


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class RecentItem(NamedTuple):
    """Лёгкая проекция находки — ровно то, что нужно для подсчёта подтверждений."""

    url_hash: str
    source_key: str
    title: str
    platform: str


class Repo:
    def __init__(self, conn):
        self.conn = conn

    # --- прогоны ----------------------------------------------------------

    def start_run(self, cadence: str) -> str:
        run_id = _uid("run")
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO monitoring_runs (run_id, cadence) VALUES (%s, %s)",
                (run_id, cadence))
        self.conn.commit()
        return run_id

    def finish_run(self, run_id: str, *, status: str = "SUCCESS", fetched: int = 0,
                   stopped: int = 0, scored: int = 0, model_calls: int = 0) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE monitoring_runs
                      SET finished_at = now(), status = %s, items_fetched = %s,
                          items_stopped = %s, items_scored = %s, model_calls = %s,
                          duration_ms = EXTRACT(EPOCH FROM now() - started_at)::int * 1000
                    WHERE run_id = %s""",
                (status, fetched, stopped, scored, model_calls, run_id))
        self.conn.commit()

    # --- находки ----------------------------------------------------------

    def is_known(self, url_hash: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM monitoring_hits WHERE url_hash = %s",
                        (url_hash,))
            return cur.fetchone() is not None

    def record_stop(self, item, verdict, run_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO stop_rule_drops
                       (drop_id, run_id, rule_code, url, title, excerpt)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (_uid("drop"), run_id, verdict.code, item.url, item.title,
                 item.body[:500]))
        self.conn.commit()

    def save_hit(self, item, result, run_id: str) -> str:
        hit_id = _uid("hit")
        backlog_until = None
        if result.decision == "BACKLOG":
            backlog_until = date.fromordinal(item.discovered_at.toordinal() + 14)

        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO monitoring_hits
                       (hit_id, run_id, url, url_hash, title, excerpt,
                        discovered_at, published_at, platforms, topics,
                        categories, source_key, source_tier, signal_key,
                        score, factors, decision, drop_reason, backlog_until, state)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           'TRIAGED')
                   ON CONFLICT (url_hash) DO NOTHING
                   RETURNING hit_id""",
                (hit_id, run_id, item.url, item.url_hash, item.title,
                 item.body[:500], item.discovered_at, item.published_at,
                 [item.platform], list(item.topics), list(item.categories),
                 item.source_key, item.tier, item.signal, result.score,
                 json.dumps(result.factors, ensure_ascii=False), result.decision,
                 "SCORE_BELOW_40" if result.decision == "DROP" else None,
                 backlog_until))
            inserted = cur.fetchone()

            if inserted is None:
                # Адрес уже в базе — вставки не было. Писать переход по
                # несуществующему hit_id нельзя: внешний ключ уронил бы весь
                # тик. Такое случается, когда один материал приходит из двух
                # лент сразу, и is_known его не поймал: он проверяет базу,
                # а не текущую пачку.
                cur.execute(
                    "SELECT hit_id FROM monitoring_hits WHERE url_hash = %s",
                    (item.url_hash,))
                existing = cur.fetchone()
                self.conn.commit()
                return existing[0] if existing else hit_id

            cur.execute(
                """INSERT INTO triage_transitions
                       (hit_id, to_decision, to_score, reason)
                   VALUES (%s, %s, %s, 'первичная оценка')""",
                (hit_id, result.decision, result.score))
        self.conn.commit()
        return hit_id

    def recent_for_confirmation(self, hours: int = 72) -> list:
        """Недавние находки для подсчёта подтверждений.

        Подтверждение нельзя искать только внутри одного тика: новость, вышедшая
        на одном сайте в 10:00, а на другом в 14:00, попадёт в разные тики и
        никогда не встретится сама с собой. Окно в трое суток покрывает
        типичное расползание сюжета по изданиям.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT url_hash, source_key, title, platforms[1]
                     FROM monitoring_hits
                    WHERE discovered_at > now() - make_interval(hours => %s)
                    ORDER BY discovered_at DESC
                    LIMIT 2000""",
                (hours,))
            return [RecentItem(url_hash=r[0], source_key=r[1], title=r[2],
                               platform=r[3] or "CROSS_PLATFORM")
                    for r in cur.fetchall()]

    # --- доставка ---------------------------------------------------------

    def _pending(self, decisions: tuple, limit: int) -> list:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT hit_id, title, url, score, decision, factors
                     FROM monitoring_hits
                    WHERE decision = ANY(%s) AND handed_off_at IS NULL
                    ORDER BY score DESC, discovered_at
                    LIMIT %s""",
                (list(decisions), limit))
            columns = [c.name for c in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def pending_urgent(self) -> list:
        return self._pending(("URGENT",), 20)

    def pending_digest(self) -> list:
        return self._pending(("QUEUE", "BACKLOG"), 50)

    def mark_delivered(self, hit_ids: list) -> None:
        if not hit_ids:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE monitoring_hits
                      SET handed_off_at = now(), state = 'HANDED_OFF'
                    WHERE hit_id = ANY(%s)""", (list(hit_ids),))
        self.conn.commit()

    def degraded_sources(self) -> list:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT query_id FROM monitoring_queries
                    WHERE consecutive_failures >= 3 AND enabled""")
            return [row[0] for row in cur.fetchall()]

    # --- heartbeat --------------------------------------------------------

    def save_heartbeat(self, report: dict, run_id=None) -> None:
        empty = report["urgent_count"] == 0 and report["queue_count"] == 0
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO heartbeat_reports
                       (report_id, run_id, answers, urgent_count, queue_count,
                        model_calls, is_empty)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (report["report_id"], run_id,
                 json.dumps(report["answers"], ensure_ascii=False),
                 report["urgent_count"], report["queue_count"],
                 report.get("model_calls", 0), empty))
        self.conn.commit()

    # --- подъём из BACKLOG ------------------------------------------------

    def promote_backlog(self, now: datetime, weights: dict, thresholds: list) -> list:
        """Находки в BACKLOG, получившие подтверждение, поднимаются в полосе.

        docs/01 §5.2: снятие штрафа плюс авторитетный источник дают +65,
        и слух на 65 баллов становится материалом на 130.
        Просроченные уходят в DROP с причиной EXPIRED_IN_BACKLOG.
        """
        from monitoring.scoring import decide, promotion_delta

        promoted = []
        delta = promotion_delta(weights)
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT hit_id, score FROM monitoring_hits
                    WHERE decision = 'BACKLOG'
                      AND factors -> 'no_confirmation' ->> 'hit' = 'true'
                      AND (backlog_until IS NULL OR backlog_until >= %s)""",
                (now.date(),))
            rows = cur.fetchall()

            for hit_id, old_score in rows:
                new_score = old_score + delta
                new_decision = decide(new_score, thresholds)
                if new_decision == "BACKLOG":
                    continue
                cur.execute(
                    """UPDATE monitoring_hits
                          SET score = %s, decision = %s, updated_at = now()
                        WHERE hit_id = %s""", (new_score, new_decision, hit_id))
                cur.execute(
                    """INSERT INTO triage_transitions
                           (hit_id, from_decision, to_decision, from_score,
                            to_score, reason)
                       VALUES (%s, 'BACKLOG', %s, %s, %s,
                               'появилось независимое подтверждение')""",
                    (hit_id, new_decision, old_score, new_score))
                promoted.append(hit_id)

            cur.execute(
                """UPDATE monitoring_hits
                      SET decision = 'DROP', state = 'DROPPED',
                          drop_reason = 'EXPIRED_IN_BACKLOG', updated_at = now()
                    WHERE decision = 'BACKLOG' AND backlog_until < %s""",
                (now.date(),))
        self.conn.commit()
        return promoted
