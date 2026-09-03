"""Доступ к Postgres: соединение, идемпотентная миграция, репозитории."""
import io
import json
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

# Импорт отложен: без него модуль не читался без установленного драйвера,
# а разбор массивов и прочая чистая логика тестируются и на машине
# разработки, где psycopg нет. Именно там и жила ошибка с «{».
def _psycopg():
    import psycopg
    return psycopg



def open_connection(dsn: str = None, **parts):
    """Долгоживущее соединение. Закрывать — вызывающему.

    Отдельно от connect() намеренно. connect() — генератор-контекстменеджер, и
    вытащить из него соединение через .__enter__(), не сохранив сам генератор,
    нельзя: сборщик мусора убирает генератор, в yield прилетает GeneratorExit,
    срабатывает finally с conn.close(), и соединение закрывается под ногами.
    Ошибка при этом приходит не в момент подключения, а на первой же операции —
    «the connection is closed», — и выглядит как проблема сети.
    """
    driver = _psycopg()
    return driver.connect(dsn, autocommit=False) if dsn \
        else driver.connect(autocommit=False, **parts)


@contextmanager
def connect(dsn: str = None, **parts):
    """Соединение по строке DATABASE_URL или по отдельным параметрам.

    Второй путь существует не для красоты: в URL пароль обязан быть
    percent-кодирован, и один символ @ или # в нём роняет разбор строки
    ещё до попытки подключиться. Ошибка при этом выглядит как невнятный
    сбой парсера, а не как «поправьте пароль». Отдельные параметры
    экранирования не требуют вовсе.
    """
    driver = _psycopg()
    conn = driver.connect(dsn, autocommit=False) if dsn \
        else driver.connect(autocommit=False, **parts)
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


# Колонки-массивы. Драйвер отдаёт массив перечисления строкой «{A,B}»,
# потому что адаптера для monitoring_platform[] у него нет. Без разбора
# list() резал такую строку посимвольно: в запрос уходил массив из «{», «W»,
# «I»… и Postgres отвечал invalid input value for enum: "{". Та же строка
# ломала выбор цвета обложки — platforms[0] был «{», и площадка всегда
# падала в запасной стиль.
ARRAY_COLUMNS = ("platforms", "topics", "categories")


def as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        # Postgres берёт элемент в кавычки, если в нём есть запятая или
        # пробел. Для перечислений такого не бывает, но полагаться на это
        # нельзя: topics — обычный text[], и там кавычки возможны.
        parts = (part.strip().strip('"') for part in value.strip("{}").split(","))
        return [part for part in parts if part]
    return list(value)


def rows_to_dicts(cur) -> list:
    """Строки курсора словарями, с разобранными массивами."""
    columns = [c.name for c in cur.description]
    out = []
    for row in cur.fetchall():
        item = dict(zip(columns, row))
        for column in ARRAY_COLUMNS:
            if column in item:
                item[column] = as_list(item[column])
        out.append(item)
    return out


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

    def rollback(self) -> None:
        """Возвращает соединение в рабочее состояние после сбоя.

        Без этого одна неудачная вставка отравляет всё соединение: Postgres
        переводит транзакцию в состояние aborted, и каждая следующая команда
        отвечает «current transaction is aborted». На боевом запуске из-за
        этого после первой ошибки посыпались все оставшиеся классы и heartbeat.
        """
        try:
            self.conn.rollback()
        except Exception:
            pass

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
                """SELECT hit_id, title, url, score, decision, factors,
                          platforms, topics, post_text
                     FROM monitoring_hits
                    WHERE decision = ANY(%s) AND handed_off_at IS NULL
                    ORDER BY score DESC, discovered_at
                    LIMIT %s""",
                (list(decisions), limit))
            return rows_to_dicts(cur)

    def pending_cards(self, bands: tuple = ("URGENT", "QUEUE")) -> list:
        """Находки, по которым редактору уходит готовый пост с кнопками.

        По умолчанию не только URGENT. Порог в 80 баллов берётся почти
        исключительно за счёт официальной ленты площадки: platform_wb +25,
        authoritative_source +15, rules_change +20. Пока эти ленты отдают
        ноль, отраслевые СМИ до 80 не дотягивают, и карточек нет вовсе —
        при том что QUEUE по словарю порогов и означает «в работу».
        """
        return self._pending(tuple(bands), 20)

    def pending_urgent(self) -> list:
        return self.pending_cards(("URGENT",))

    def pending_digest(self, bands: tuple = ("BACKLOG",)) -> list:
        """Дайджест — то, что не пошло карточкой.

        Пересекаться с карточками он не должен: иначе одна находка приходит
        и постом, и строкой в списке, и редактор дважды принимает решение
        по одному материалу.
        """
        return self._pending(tuple(bands), 50)

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

    def save_post_text(self, hit_id: str, text: str) -> None:
        """Готовый текст поста к находке.

        Публиковать надо ровно то, что редактор одобрил. Вторая генерация
        в момент нажатия дала бы другой текст, и в канал ушло бы
        несогласованное.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE monitoring_hits SET post_text = %s, updated_at = now() "
                " WHERE hit_id = %s", (text, hit_id))
        self.conn.commit()

    # --- решения редактора ------------------------------------------------

    def save_decision(self, hit_id: str, action: str, chat_id: str,
                      message_id=None, prompt_message_id=None,
                      editor_id=None) -> str:
        """Пишет нажатие кнопки. Возвращает decision_id."""
        decision_id = _uid("dec")
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO moderation_decisions
                       (decision_id, hit_id, action, chat_id, message_id,
                        prompt_message_id, editor_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (decision_id, hit_id, action, str(chat_id), message_id,
                 prompt_message_id, editor_id and str(editor_id)))
        self.conn.commit()
        return decision_id

    def hit_id_awaiting_note(self, prompt_message_id: int):
        """Находка, к которой редактор пишет правку, или None.

        Берётся последнее приглашение без заполненной правки: если редактор
        нажал «Редактировать» дважды, ответ относится к тому приглашению,
        на которое он ответил, а не к первому по времени.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT decision_id, hit_id FROM moderation_decisions
                    WHERE prompt_message_id = %s AND editor_note IS NULL
                    ORDER BY decided_at DESC LIMIT 1""",
                (prompt_message_id,))
            row = cur.fetchone()
        return (row[0], row[1]) if row else None

    def save_note(self, decision_id: str, note: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE moderation_decisions SET editor_note = %s "
                "WHERE decision_id = %s", (note, decision_id))
        self.conn.commit()

    def set_hit_state(self, hit_id: str, state: str) -> None:
        """Перевод находки в состояние по решению редактора.

        Находка из проверки связи в таблице отсутствует — UPDATE просто
        не заденет ни одной строки, и это правильно: падать не на чем.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE monitoring_hits
                      SET state = %s, updated_at = now(),
                          handed_off_at = COALESCE(handed_off_at,
                              CASE WHEN %s = 'HANDED_OFF' THEN now() END)
                    WHERE hit_id = %s""", (state, state, hit_id))
        self.conn.commit()

    def hit_by_id(self, hit_id: str):
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT hit_id, title, url, score, decision, factors,
                          platforms, topics, post_text
                     FROM monitoring_hits WHERE hit_id = %s""", (hit_id,))
            found = rows_to_dicts(cur)
            return found[0] if found else None

    # --- пересчёт ---------------------------------------------------------

    def band_counts(self) -> dict:
        """Сколько находок в каждой полосе. Без этого «постов нет» неотличимо
        от «нечему уходить»: первое чинят кодом, второе — источниками."""
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(decision::text, 'без оценки') AS band,
                          count(*) AS n
                     FROM monitoring_hits
                    GROUP BY 1 ORDER BY 2 DESC""")
            return {row[0]: row[1] for row in cur.fetchall()}

    def hits_to_rescore(self, hours: int, limit: int) -> list:
        """Недооценённые находки, которые стоит пересчитать.

        Корпус набирался, пока классификатор был недоступен: без семи
        факторов из четырнадцати потолок — 65 из 160, а со штрафом −50
        за отсутствие подтверждения почти всё уходило в DROP. Эти находки
        не плохи — они не размечены, и заново их не соберёт никто:
        is_known не пустит те же адреса во второй раз.

        Берётся не только DROP. Без факторов-суждений типичная находка
        набирает около 55 — это BACKLOG, и до QUEUE ей не хватает пяти
        баллов. Ограничься мы отброшенными, мимо прошли бы как раз те,
        кому подняться проще всего.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT hit_id, url, url_hash, title, excerpt, source_key,
                          source_tier, platforms, topics, discovered_at,
                          published_at, decision
                     FROM monitoring_hits
                    WHERE decision IN ('DROP', 'BACKLOG')
                      AND discovered_at > now() - make_interval(hours => %s)
                      AND handed_off_at IS NULL
                    ORDER BY score DESC NULLS LAST, discovered_at DESC
                    LIMIT %s""", (hours, limit))
            return rows_to_dicts(cur)

    def update_score(self, hit_id: str, result, reason: str) -> None:
        """Новая оценка находки с записью перехода.

        Переход пишется всегда: по нему видно, что полоса сменилась
        не сама собой, а из-за пересчёта.
        """
        with self.conn.cursor() as cur:
            cur.execute("SELECT score, decision FROM monitoring_hits "
                        " WHERE hit_id = %s", (hit_id,))
            row = cur.fetchone()
            old_score, old_decision = (row or (None, None))

            cur.execute(
                """UPDATE monitoring_hits
                      SET score = %s, factors = %s, decision = %s,
                          drop_reason = CASE WHEN %s = 'DROP'
                                             THEN COALESCE(drop_reason, %s)
                                             ELSE NULL END,
                          updated_at = now()
                    WHERE hit_id = %s""",
                (result.score,
                 json.dumps(result.factors, ensure_ascii=False), result.decision,
                 result.decision, reason, hit_id))
            cur.execute(
                # transition_id не указывается: колонка bigserial, номер
                # выдаёт база. Подставленный сюда текстовый идентификатор
                # ронял весь пересчёт — invalid input syntax for type bigint.
                """INSERT INTO triage_transitions
                       (hit_id, from_decision, to_decision,
                        from_score, to_score, reason)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (hit_id, old_decision, result.decision,
                 old_score, result.score, reason))
        self.conn.commit()

    # --- разборы ----------------------------------------------------------

    def explainer_done(self, slot: str) -> bool:
        """Был ли уже разбор в этот выпуск сегодня."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM explainers "
                " WHERE slot = %s AND produced_date = current_date", (slot,))
            return cur.fetchone() is not None

    def explainer_candidates(self, hours: int, limit: int = 5) -> list:
        """Темы для разбора: лучшее за окно, о чём ещё не говорили.

        Полоса очереди здесь не фильтрует, и это не небрежность. В таблицу
        находок попадает только то, что прошло стоп-правила: реклама, вода
        и обрывки отсеиваются раньше и лежат в stop_rule_drops. DROP означает
        «мало баллов», а не «хлам», — и в первую очередь мало их у того, что
        размечалось, пока классификатор был недоступен: без семи факторов
        из четырнадцати потолок — 65 из 160.

        А для разбора важна не срочность, а польза: толковое объяснение правил
        вырастает и из находки, которая в срочные не прошла. От пустых тем
        защищает не порог, а требование к объёму материала в explainer.py.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT hit_id, title, url, score, decision, platforms, topics
                     FROM monitoring_hits
                    WHERE discovered_at > now() - make_interval(hours => %s)
                      AND score IS NOT NULL
                      AND hit_id NOT IN (SELECT hit_id FROM explainers)
                    ORDER BY score DESC, discovered_at DESC
                    LIMIT %s""", (hours, limit))
            return rows_to_dicts(cur)

    def related_hits(self, hit: dict, hours: int, limit: int = 6) -> list:
        """Другие находки той же площадки за окно — материал для глубины.

        Одна новость даёт повод, но не даёт инструкции: официальное
        уведомление говорит, что меняется, а разбор отраслевого издания —
        чем это грозит.

        Приведение к monitoring_platform[] обязательно. Без него Postgres
        разбирает переданный массив как одиночное значение перечисления
        и падает на первой же скобке: invalid input value for enum
        monitoring_platform: "{".
        """
        platforms = as_list(hit.get("platforms"))
        if not platforms:
            return []

        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT hit_id, title, url
                     FROM monitoring_hits
                    WHERE discovered_at > now() - make_interval(hours => %s)
                      AND hit_id <> %s
                      AND platforms && %s::monitoring_platform[]
                    ORDER BY score DESC
                    LIMIT %s""",
                (hours, hit.get("hit_id", ""), platforms, limit))
            return rows_to_dicts(cur)

    def save_explainer(self, hit_id: str, slot: str, topic: str,
                       body: str) -> str:
        explainer_id = _uid("exp")
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO explainers
                       (explainer_id, hit_id, slot, topic, body)
                   VALUES (%s, %s, %s, %s, %s)""",
                (explainer_id, hit_id, slot, topic, body))
        self.conn.commit()
        return explainer_id

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
