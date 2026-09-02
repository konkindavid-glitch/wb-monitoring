-- ---------------------------------------------------------------------------
--  Карта мониторинга — начальная схема
--
--  Обоснование решений:
--    docs/00-monitoring-map.md   таксономия
--    docs/01-triage-scoring.md   матрица и пороги
--    docs/02-cadence.md          классы частоты
--    docs/03-handoff.md          контракт передачи
--
--  Применение:  psql -f sql/001_monitoring_map.sql
-- ---------------------------------------------------------------------------

BEGIN;

-- ---------------------------------------------------------------------------
--  1. ПЕРЕЧИСЛЕНИЯ
-- ---------------------------------------------------------------------------

DO $$ BEGIN
    CREATE TYPE monitoring_platform AS ENUM (
      'WILDBERRIES','OZON','YANDEX_MARKET','MEGAMARKET','AVITO','LAMODA',
      'ALIEXPRESS_RU','KUPER','DETSKIY_MIR','LEMANA_PRO','HOFF','VSEINSTRUMENTI',
      'CITILINK','MVIDEO','DNS','NICHE','CROSS_PLATFORM','OTHER'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Зеркало event_category из соседнего проекта (C:\Ведение группы в телеграм).
-- Общий словарь выбран сознательно: он позволит состыковать карту с готовым
-- pipeline без переразметки накопленного. См. docs/00-monitoring-map.md §3.
DO $$ BEGIN
    CREATE TYPE monitoring_category AS ENUM (
      'COMMISSION_TARIFF','PENALTY_DEDUCTION','OFFER_RULES','LOGISTICS','RANKING_ALGO',
      'ADVERTISING','PAYMENTS_SETTLEMENT','TAXES','REGULATION_LAW','ANTITRUST_FAS',
      'COURT_PRACTICE','PLATFORM_TOOLS','AI_TECH','MARKET_TREND','SELLER_CASE',
      'INCIDENT_OUTAGE','BUYER_IMPACT','BRAND_IP'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE cadence_class AS ENUM ('A','B','C','D','E','F');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE triage_decision AS ENUM ('URGENT','QUEUE','BACKLOG','DROP');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE hit_state AS ENUM ('NEW','SCORED','TRIAGED','HANDED_OFF','DROPPED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE signal_method AS ENUM (
      'doc_diff','mass_detector','metric_jump','cabinet_snapshot_diff',
      'registry_watch','api_changelog_diff','editorial_pick'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------------------
--  2. ТАКСОНОМИЯ
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS monitoring_topics (
    topic_key       text PRIMARY KEY,
    parent_key      text REFERENCES monitoring_topics (topic_key),
    title           text NOT NULL,
    category        monitoring_category,          -- NULL у верхнего уровня
    sort_order      integer NOT NULL DEFAULT 0,
    enabled         boolean NOT NULL DEFAULT true,

    -- Верхний уровень не имеет категории; подтема обязана её иметь.
    CONSTRAINT topic_category_by_level CHECK (
        (parent_key IS NULL AND category IS NULL) OR
        (parent_key IS NOT NULL AND category IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_topics_1
    ON monitoring_topics (parent_key);
CREATE INDEX IF NOT EXISTS idx_topics_2
    ON monitoring_topics (category) WHERE category IS NOT NULL;

CREATE TABLE IF NOT EXISTS monitoring_platforms (
    platform        monitoring_platform PRIMARY KEY,
    title           text NOT NULL,
    priority        text NOT NULL CHECK (priority IN ('P1','P2','P3','P4')),
    -- Тай-брейк очереди, НЕ баллы матрицы. См. docs/00-monitoring-map.md §2.2.
    priority_value  smallint NOT NULL CHECK (priority_value BETWEEN 0 AND 25),
    enabled         boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS monitoring_signals (
    signal_key      text PRIMARY KEY,
    title           text NOT NULL,
    method          signal_method NOT NULL,
    enabled         boolean NOT NULL DEFAULT true
);

-- ---------------------------------------------------------------------------
--  3. ЗАПРОСЫ И РАСПИСАНИЕ
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS monitoring_queries (
    query_id        text PRIMARY KEY,
    group_key       text NOT NULL,
    query_text      text NOT NULL,
    topic_key       text NOT NULL REFERENCES monitoring_topics (topic_key),
    platform        monitoring_platform NOT NULL,
    cadence         cadence_class NOT NULL,
    enabled         boolean NOT NULL DEFAULT true,
    last_run_at     timestamptz,
    last_ok_at      timestamptz,
    consecutive_failures integer NOT NULL DEFAULT 0,

    UNIQUE (group_key, query_text)
);
CREATE INDEX IF NOT EXISTS idx_queries_1
    ON monitoring_queries (cadence, enabled, last_run_at NULLS FIRST);
CREATE INDEX IF NOT EXISTS idx_queries_2
    ON monitoring_queries (group_key);

-- Каждый тик, включая пустой: без записи пустых тиков невозможно отличить
-- «ничего не происходило» от «мониторинг стоял».
CREATE TABLE IF NOT EXISTS monitoring_runs (
    run_id          text PRIMARY KEY,
    cadence         cadence_class NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    duration_ms     integer,
    items_fetched   integer NOT NULL DEFAULT 0,
    items_stopped   integer NOT NULL DEFAULT 0,   -- отсеяно стоп-правилами
    items_scored    integer NOT NULL DEFAULT 0,
    model_calls     integer NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT 'RUNNING'
                    CHECK (status IN ('RUNNING','SUCCESS','FAILED','SKIPPED_OVERLAP','DEGRADED')),
    error           text
);
CREATE INDEX IF NOT EXISTS idx_runs_1
    ON monitoring_runs (cadence, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_2
    ON monitoring_runs (status, started_at DESC)
    WHERE status IN ('FAILED','SKIPPED_OVERLAP','DEGRADED');

-- ---------------------------------------------------------------------------
--  4. НАХОДКИ
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS monitoring_hits (
    hit_id          text PRIMARY KEY,
    run_id          text REFERENCES monitoring_runs (run_id),
    query_id        text REFERENCES monitoring_queries (query_id),
    signal_key      text REFERENCES monitoring_signals (signal_key),

    url             text NOT NULL,
    url_hash        text NOT NULL,                -- для быстрого поиска дублей
    title           text NOT NULL,
    excerpt         text,

    discovered_at   timestamptz NOT NULL DEFAULT now(),
    published_at    timestamptz,
    effective_date  date,

    platforms       monitoring_platform[] NOT NULL DEFAULT '{}',
    topics          text[]                NOT NULL DEFAULT '{}',
    categories      monitoring_category[] NOT NULL DEFAULT '{}',

    source_key      text NOT NULL,
    source_tier     text CHECK (source_tier IN ('T1','T2','T3','T4','T5','T6')),
    snapshot_id     text,

    score           integer,
    -- Разбор по всем 14 факторам, а не только сумма: при перекалибровке весов
    -- накопленные находки пересчитываются без повторного прохода.
    -- См. docs/01-triage-scoring.md §6.
    factors         jsonb,
    decision        triage_decision,
    drop_reason     text,

    state           hit_state NOT NULL DEFAULT 'NEW',
    backlog_until   date,                         -- срок жизни в BACKLOG
    handed_off_at   timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT score_within_bounds CHECK (score IS NULL OR score BETWEEN -180 AND 160),
    -- Посчитанная находка обязана иметь разбор: сумма без разбора непересчитываема.
    CONSTRAINT scored_has_factors CHECK (
        state = 'NEW' OR (score IS NOT NULL AND factors IS NOT NULL AND decision IS NOT NULL)
    ),
    CONSTRAINT dropped_has_reason CHECK (
        decision IS DISTINCT FROM 'DROP' OR drop_reason IS NOT NULL
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hits_1
    ON monitoring_hits (url_hash);
CREATE INDEX IF NOT EXISTS idx_hits_2
    ON monitoring_hits (decision, score DESC) WHERE state <> 'DROPPED';
CREATE INDEX IF NOT EXISTS idx_hits_3
    ON monitoring_hits (state, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_hits_4
    ON monitoring_hits (backlog_until) WHERE decision = 'BACKLOG';
CREATE INDEX IF NOT EXISTS idx_hits_5
    ON monitoring_hits (effective_date) WHERE effective_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_hits_6
    ON monitoring_hits USING gin (topics);
CREATE INDEX IF NOT EXISTS idx_hits_7
    ON monitoring_hits USING gin (platforms);
CREATE INDEX IF NOT EXISTS idx_hits_8
    ON monitoring_hits USING gin (factors);

-- ---------------------------------------------------------------------------
--  5. ОТСЕВ ПО СТОП-ПРАВИЛАМ
--
--  Отдельная таблица, а не флаг в monitoring_hits: отсев происходит ДО подсчёта
--  баллов, и такая запись не является находкой. Хранится, чтобы можно было
--  померить, сколько нужного выброшено — правило, молча съедающее 3% полезного
--  потока, выглядит как отсутствие новостей.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS stop_rule_drops (
    drop_id         text PRIMARY KEY,
    run_id          text REFERENCES monitoring_runs (run_id),
    query_id        text REFERENCES monitoring_queries (query_id),
    rule_code       text NOT NULL,
    url             text,
    title           text,
    excerpt         text,
    dropped_at      timestamptz NOT NULL DEFAULT now(),
    reviewed        boolean NOT NULL DEFAULT false,
    review_verdict  text CHECK (review_verdict IN ('CORRECT','FALSE_DROP'))
);
CREATE INDEX IF NOT EXISTS idx_stopruledrops_1
    ON stop_rule_drops (rule_code, dropped_at DESC);
CREATE INDEX IF NOT EXISTS idx_stopruledrops_2
    ON stop_rule_drops (reviewed) WHERE reviewed = false;

-- ---------------------------------------------------------------------------
--  6. HEARTBEAT
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS heartbeat_reports (
    report_id       text PRIMARY KEY,
    run_id          text REFERENCES monitoring_runs (run_id),
    tick_at         timestamptz NOT NULL DEFAULT now(),
    -- Массив из десяти ответов по heartbeat.schema.json.
    -- data_age_seconds обязателен у каждого: ответ «нет» без возраста
    -- неотличим от «не проверяли». См. docs/02-cadence.md §3.
    answers         jsonb NOT NULL,
    urgent_count    integer NOT NULL DEFAULT 0,
    queue_count     integer NOT NULL DEFAULT 0,
    model_calls     integer NOT NULL DEFAULT 0,
    is_empty        boolean NOT NULL DEFAULT false,   -- для схлопывания в отчёте

    CONSTRAINT answers_is_array CHECK (jsonb_typeof(answers) = 'array'),
    CONSTRAINT answers_count_ten CHECK (jsonb_array_length(answers) = 10)
);
CREATE INDEX IF NOT EXISTS idx_heartbeatrepor_1
    ON heartbeat_reports (tick_at DESC);
CREATE INDEX IF NOT EXISTS idx_heartbeatrepor_2
    ON heartbeat_reports (tick_at DESC) WHERE is_empty = false;

-- ---------------------------------------------------------------------------
--  7. ИСТОРИЯ РЕШЕНИЙ
--
--  Append-only: полоса находки меняется при подъёме из BACKLOG, и нужно видеть,
--  что именно изменилось и почему.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS triage_transitions (
    transition_id   bigserial PRIMARY KEY,
    hit_id          text NOT NULL REFERENCES monitoring_hits (hit_id),
    from_decision   triage_decision,
    to_decision     triage_decision NOT NULL,
    from_score      integer,
    to_score        integer NOT NULL,
    reason          text NOT NULL,
    occurred_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_triagetransiti_1
    ON triage_transitions (hit_id, occurred_at);

-- ---------------------------------------------------------------------------
--  8. ПРАВКИ ПОВЕРХ УЖЕ СОЗДАННОЙ СХЕМЫ
--  Идемпотентны: применяются и к чистой базе, и к существующей.
-- ---------------------------------------------------------------------------

-- origin_present требовал у находки query_id или signal_key. Это неверно:
-- материал из RSS-ленты приходит напрямую, без того и другого, и вставка
-- падала на первом же элементе. Происхождение у находки есть всегда —
-- это source_key, и он объявлен NOT NULL. Ограничение лишнее.
ALTER TABLE monitoring_hits DROP CONSTRAINT IF EXISTS origin_present;

-- ---------------------------------------------------------------------------
--  9. ЖУРНАЛ РЕШЕНИЙ РЕДАКТОРА
--  Что нажали под карточкой: запостить, отредактировать, удалить.
-- ---------------------------------------------------------------------------

-- Внешнего ключа на monitoring_hits намеренно нет. Журнал должен принимать
-- решение по любой карточке — в том числе по эталонной из проверки связи,
-- которой в таблице находок нет, и по находке, вычищенной ретеншеном.
-- Внешний ключ здесь уронил бы запись решения, а решение редактора — это
-- то, по чему потом калибруются пороги; терять его нельзя.
CREATE TABLE IF NOT EXISTS moderation_decisions (
    decision_id       text PRIMARY KEY,
    hit_id            text NOT NULL,
    action            text NOT NULL CHECK (action IN ('pub','edit','del')),
    chat_id           text NOT NULL,
    message_id        bigint,
    -- Сообщение-приглашение к правке: по ответу на него находится находка.
    prompt_message_id bigint,
    editor_id         text,
    editor_note       text,
    decided_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_moderationdec_1
    ON moderation_decisions (hit_id, decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_moderationdec_2
    ON moderation_decisions (prompt_message_id)
    WHERE prompt_message_id IS NOT NULL;

-- ---------------------------------------------------------------------------
--  10. РАЗБОРЫ
--  Длинный пост-объяснение по одной теме, дважды в день.
-- ---------------------------------------------------------------------------

-- Внешнего ключа на monitoring_hits нет по той же причине, что и у журнала
-- решений: разбор переживает чистку находок, а по нему видно, о чём канал
-- уже говорил. Без этой памяти одна и та же тема разбиралась бы каждый день.
CREATE TABLE IF NOT EXISTS explainers (
    explainer_id  text PRIMARY KEY,
    hit_id        text NOT NULL,
    slot          text NOT NULL,          -- время выпуска, например 10:00
    topic         text NOT NULL,
    body          text NOT NULL,
    produced_at   timestamptz NOT NULL DEFAULT now(),
    produced_date date NOT NULL DEFAULT current_date
);
CREATE INDEX IF NOT EXISTS idx_explainers_1
    ON explainers (produced_date DESC, slot);
CREATE INDEX IF NOT EXISTS idx_explainers_2
    ON explainers (hit_id);

COMMIT;
