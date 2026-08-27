# Сборщик «Карты мониторинга» v1 — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собрать рабочий тракт от опроса источников до дайджеста в Telegram, чтобы матрица триажа впервые отработала на живых данных и её пороги можно было откалибровать.

**Architecture:** Python-сервис с шестью классами частоты опроса. Два коллектора (сравнение снапшотов страниц и RSS) наполняют Postgres; детерминированные стоп-правила отсеивают мусор до подсчёта баллов; 14 факторов матрицы проставляются гибридно — семь кодом, семь через Claude пачками; чистая функция `scoring.py` считает сумму и полосу очереди. Раз в пять минут строится heartbeat из десяти вопросов и уходит доставка.

**Tech Stack:** Python 3.12, PostgreSQL (Amvera managed), psycopg 3, httpx, feedparser, BeautifulSoup + lxml, anthropic, pytest.

**Spec:** [docs/superpowers/specs/2026-08-27-monitoring-collector-design.md](../specs/2026-08-27-monitoring-collector-design.md)

## Global Constraints

- Python 3.12; тулчейн Амверы — `pip`.
- Кодовая страница машины разработки — cp1251. Файлы с кириллицей создавать инструментом Write, не heredoc'ом. Python-скрипты запускать с `PYTHONUTF8=1`. Файлы читать и писать через `io.open(..., encoding='utf-8')`.
- Веса матрицы и пороги 80/60/40 менять **нельзя** — они заданы в `config/triage.yaml` и являются требованием. Код читает их оттуда, не хардкодит.
- Набор `monitoring_category` — ровно 18 значений из `sql/001_monitoring_map.sql`. Добавление значения — отдельное решение, не рабочая правка.
- Секреты только из переменных окружения: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. В репозиторий не попадают никогда.
- Обоснование (`why`) обязательно у каждого сработавшего фактора — фактор без него не засчитывается.
- Стоп-правила срабатывают **до** подсчёта баллов.
- Каждое срабатывание стоп-правила пишется в `stop_rule_drops`. Молчаливый отсев запрещён.

---

## Структура файлов

| Файл | Ответственность |
|---|---|
| `requirements.txt` | Зависимости |
| `app.py` | Точка входа: `--once` и режим цикла, расписание классов A–F |
| `monitoring/config.py` | Загрузка и валидация четырёх YAML |
| `monitoring/models.py` | `SourceItem`, `ScoreResult`, `StopVerdict` — общие типы |
| `monitoring/db.py` | Пул к Postgres, идемпотентная миграция, репозитории |
| `monitoring/scoring.py` | Чистая матрица: сумма, полоса, тай-брейк |
| `monitoring/stop_rules.py` | 14 детерминированных правил |
| `monitoring/topics.py` | Разметка тем и категорий по 89 запросам — шов под поиск |
| `monitoring/normalize.py` | Любой источник → `SourceItem` |
| `monitoring/collectors/base.py` | HTTP: retry, rate limit, ETag |
| `monitoring/collectors/rss.py` | RSS через feedparser |
| `monitoring/collectors/doc_diff.py` | Снапшот → нормализация DOM → diff |
| `monitoring/collectors/onboarding.py` | Проверка источника перед вводом в строй |
| `monitoring/factors/mechanical.py` | 7 факторов кодом |
| `monitoring/factors/judgment.py` | 7 факторов через Claude |
| `monitoring/heartbeat.py` | 10 вопросов с возрастом данных |
| `monitoring/delivery.py` | Дайджест и срочные находки в Telegram |
| `config/sources.yaml` | Реестр источников v1 |
| `amvera.yml` | Конфигурация деплоя |
| `tests/` | Зеркало структуры `monitoring/` |

---

## Task 1: Каркас и загрузка конфигов

**Files:**
- Create: `requirements.txt`, `monitoring/__init__.py`, `monitoring/config.py`, `monitoring/models.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: существующие `config/monitoring-map.yaml`, `config/queries.yaml`, `config/triage.yaml`
- Produces: `load_config(root: Path) -> Config`; `Config` с полями `.map`, `.queries`, `.triage`, `.sources` (dict каждый), методами `.factor_weights() -> dict[str,int]`, `.thresholds() -> list[dict]`, `.stop_rule_codes() -> set[str]`, `.topic_keys() -> set[str]`, `.platform_priority(key) -> int`

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_config.py
from pathlib import Path
import pytest
from monitoring.config import load_config, ConfigError

ROOT = Path(__file__).resolve().parents[1]

def test_loads_all_configs():
    cfg = load_config(ROOT)
    assert len(cfg.factor_weights()) == 14
    assert cfg.factor_weights()["platform_wb"] == 25
    assert cfg.factor_weights()["no_confirmation"] == -50
    assert len(cfg.stop_rule_codes()) == 14
    assert len(cfg.topic_keys()) == 11
    assert cfg.platform_priority("WILDBERRIES") == 25
    assert cfg.platform_priority("OZON") == 15

def test_thresholds_are_ordered_descending():
    bands = load_config(ROOT).thresholds()
    scores = [b["min_score"] for b in bands if b["min_score"] is not None]
    assert scores == [80, 60, 40]

def test_rejects_config_with_wrong_factor_count(tmp_path):
    # Матрица из 13 факторов — требование нарушено, грузить нельзя
    (tmp_path / "config").mkdir()
    for name in ("monitoring-map.yaml", "queries.yaml"):
        (tmp_path / "config" / name).write_text(
            (ROOT / "config" / name).read_text(encoding="utf-8"), encoding="utf-8")
    triage = (ROOT / "config" / "triage.yaml").read_text(encoding="utf-8")
    triage = triage.replace('  - key: is_advertising\n    title: "Рекламный материал"\n    weight: -60\n', "")
    (tmp_path / "config" / "triage.yaml").write_text(triage, encoding="utf-8")
    with pytest.raises(ConfigError, match="14"):
        load_config(tmp_path)
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitoring'`

- [ ] **Step 3: Написать зависимости и минимальную реализацию**

```
# requirements.txt
psycopg[binary]==3.2.3
httpx==0.28.1
feedparser==6.0.12
beautifulsoup4==4.15.0
lxml==6.1.1
PyYAML==6.0.3
anthropic==0.40.0
jsonschema==4.26.0
pytest==8.3.4
```

```python
# monitoring/config.py
"""Загрузка и валидация конфигурации карты мониторинга.

Веса и пороги — требование, а не настройка по умолчанию. Конфиг, который им
не соответствует, не грузится: лучше не запуститься, чем молча считать по
другой матрице.
"""
import io
from dataclasses import dataclass
from pathlib import Path

import yaml

EXPECTED_FACTORS = 14
EXPECTED_STOP_RULES = 14
EXPECTED_TOPICS = 11


class ConfigError(Exception):
    pass


def _read_yaml(path: Path):
    if not path.exists():
        raise ConfigError(f"нет файла конфигурации: {path}")
    with io.open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass(frozen=True)
class Config:
    map: dict
    queries: dict
    triage: dict
    sources: dict

    def factor_weights(self) -> dict:
        return {f["key"]: f["weight"] for f in self.triage["factors"]}

    def thresholds(self) -> list:
        return self.triage["thresholds"]

    def stop_rule_codes(self) -> set:
        return {r["code"] for r in self.triage["stop_rules"]}

    def topic_keys(self) -> set:
        return {t["key"] for t in self.map["topics"]}

    def platform_priority(self, key: str) -> int:
        for p in self.map["platforms"]:
            if p["key"] == key:
                return p["priority_value"]
        raise ConfigError(f"неизвестная площадка: {key}")


def load_config(root: Path) -> Config:
    cfg = Config(
        map=_read_yaml(root / "config" / "monitoring-map.yaml"),
        queries=_read_yaml(root / "config" / "queries.yaml"),
        triage=_read_yaml(root / "config" / "triage.yaml"),
        sources=_read_yaml(root / "config" / "sources.yaml")
        if (root / "config" / "sources.yaml").exists() else {"sources": []},
    )
    weights = cfg.factor_weights()
    if len(weights) != EXPECTED_FACTORS:
        raise ConfigError(
            f"в матрице должно быть {EXPECTED_FACTORS} факторов, найдено {len(weights)}")
    if len(cfg.stop_rule_codes()) != EXPECTED_STOP_RULES:
        raise ConfigError(
            f"должно быть {EXPECTED_STOP_RULES} стоп-правил, "
            f"найдено {len(cfg.stop_rule_codes())}")
    if len(cfg.topic_keys()) != EXPECTED_TOPICS:
        raise ConfigError(
            f"должно быть {EXPECTED_TOPICS} тем, найдено {len(cfg.topic_keys())}")
    return cfg
```

```python
# monitoring/__init__.py
```

```python
# monitoring/models.py
"""Общие типы, которые ходят между модулями."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SourceItem:
    source_key: str
    url: str
    url_hash: str
    title: str
    body: str
    discovered_at: datetime
    published_at: datetime | None = None
    tier: str = "T3"
    platform: str = "CROSS_PLATFORM"
    signal: str | None = None
    topics: tuple = ()
    categories: tuple = ()


@dataclass(frozen=True)
class ScoreResult:
    score: int
    decision: str
    factors: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StopVerdict:
    code: str | None = None
    detail: str | None = None

    @property
    def stopped(self) -> bool:
        return self.code is not None
```

- [ ] **Step 4: Запустить тесты — должны пройти**

Run: `PYTHONUTF8=1 python -m pytest tests/test_config.py -v`
Expected: PASS, 3 теста

- [ ] **Step 5: Коммит**

```bash
git add requirements.txt monitoring/ tests/test_config.py
git commit -m "feat: загрузка и валидация конфигурации карты мониторинга"
```

---

## Task 2: Матрица оценки — чистая функция

Самая важная задача плана. Здесь живёт вся матрица Давида, и именно её поведение нужно было проверить с самого начала.

**Files:**
- Create: `monitoring/scoring.py`
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `Config.factor_weights()`, `Config.thresholds()` из Task 1
- Produces: `score_item(fired: dict[str, str], weights: dict[str, int], thresholds: list[dict]) -> ScoreResult`; `decide(score: int, thresholds: list[dict]) -> str`; `promotion_delta(weights) -> int`

`fired` — словарь `{ключ_фактора: обоснование}`. Ключ отсутствует — фактор не сработал. Пустое обоснование — ошибка.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_scoring.py
from pathlib import Path
import pytest
from monitoring.config import load_config
from monitoring.scoring import score_item, decide, promotion_delta

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT)
W = CFG.factor_weights()
T = CFG.thresholds()


def s(fired):
    return score_item(fired, W, T)


def test_wb_tariff_change_is_urgent():
    """Эталонный случай 1 из docs/01-triage-scoring.md §2.3."""
    r = s({
        "platform_wb": "раздел тарифов WB",
        "seller_money_impact": "прямые расходы на хранение",
        "rules_change": "новая редакция тарифной таблицы",
        "authoritative_source": "официальный раздел площадки",
        "is_fresh": "обнаружено через 6 минут",
        "has_practical_takeaway": "пересчёт себестоимости хранения",
        "mass_effect": "затронуты все FBO-поставки",
    })
    assert r.score == 130
    assert r.decision == "URGENT"


def test_ai_news_without_platform_is_backlog():
    """Эталонный случай 2."""
    r = s({
        "ai_link": "AI-инструмент для карточек",
        "authoritative_source": "блог вендора",
        "is_fresh": "вчера",
        "has_practical_takeaway": "применимо к описаниям",
    })
    assert r.score == 55
    assert r.decision == "BACKLOG"


def test_unconfirmed_rumour_still_reaches_queue():
    """Эталонный случай 3 — тот самый, ради которого матрица не решает публикацию.

    Слух набирает 65 баллов и попадает в рабочую полосу. Это не баг матрицы,
    а её свойство: взвешенная сумма допускает компенсацию. Защита — в том,
    что QUEUE означает «проверить первым», а не «публиковать».
    """
    r = s({
        "platform_wb": "речь про WB",
        "seller_money_impact": "новый сбор",
        "rules_change": "меняются условия",
        "is_fresh": "сегодня",
        "has_practical_takeaway": "пересчитать цены",
        "mass_effect": "пишут многие",
        "no_confirmation": "источник назван, независимых подтверждений нет",
    })
    assert r.score == 65
    assert r.decision == "QUEUE"


@pytest.mark.parametrize("score,expected", [
    (160, "URGENT"), (80, "URGENT"), (79, "QUEUE"),
    (60, "QUEUE"), (59, "BACKLOG"), (40, "BACKLOG"),
    (39, "DROP"), (0, "DROP"), (-180, "DROP"),
])
def test_band_boundaries(score, expected):
    assert decide(score, T) == expected


def test_backlog_promotion_turns_rumour_into_urgent():
    """docs/01 §5.2: подтверждение снимает −50 и добавляет +15."""
    rumour = s({
        "platform_wb": "WB", "seller_money_impact": "сбор",
        "rules_change": "условия", "is_fresh": "сегодня",
        "has_practical_takeaway": "пересчитать", "mass_effect": "многие",
        "no_confirmation": "нет подтверждения",
    })
    assert promotion_delta(W) == 65
    promoted = rumour.score + promotion_delta(W)
    assert promoted == 130
    assert decide(promoted, T) == "URGENT"


def test_factors_breakdown_is_complete_and_zeroed():
    r = s({"platform_wb": "WB"})
    assert len(r.factors) == 14
    assert r.factors["platform_wb"] == {"hit": True, "weight": 25, "why": "WB"}
    assert r.factors["is_advertising"] == {"hit": False, "weight": 0}


def test_fired_factor_without_rationale_is_rejected():
    with pytest.raises(ValueError, match="обоснование"):
        s({"platform_wb": ""})


def test_unknown_factor_is_rejected():
    with pytest.raises(ValueError, match="неизвестный фактор"):
        s({"platform_yandex": "нет такого"})
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_scoring.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitoring.scoring'`

- [ ] **Step 3: Написать минимальную реализацию**

```python
# monitoring/scoring.py
"""Матрица оценки: сумма баллов и полоса очереди.

Модуль намеренно чистый — никакого ввода-вывода. Вся матрица из ТЗ живёт
здесь и покрывается тестами без сети и базы.

ВАЖНО: сумма определяет ПРИОРИТЕТ ОЧЕРЕДИ, а не разрешение на публикацию.
Взвешенная сумма допускает компенсацию: слух без подтверждения набирает
65 баллов. Публикацию решают четыре независимых гейта через AND, они живут
downstream. См. docs/01-triage-scoring.md §3.
"""
from monitoring.models import ScoreResult


def decide(score: int, thresholds: list) -> str:
    for band in thresholds:
        if band["min_score"] is not None and score >= band["min_score"]:
            return band["decision"]
    return "DROP"


def promotion_delta(weights: dict) -> int:
    """Прибавка при появлении независимого подтверждения.

    Снимается штраф no_confirmation и добавляется authoritative_source.
    """
    return -weights["no_confirmation"] + weights["authoritative_source"]


def score_item(fired: dict, weights: dict, thresholds: list) -> ScoreResult:
    factors = {}
    total = 0
    for key, weight in weights.items():
        why = fired.get(key)
        if key in fired:
            if not why or not str(why).strip():
                raise ValueError(
                    f"у сработавшего фактора {key} должно быть обоснование")
            factors[key] = {"hit": True, "weight": weight, "why": why}
            total += weight
        else:
            factors[key] = {"hit": False, "weight": 0}

    unknown = set(fired) - set(weights)
    if unknown:
        raise ValueError(f"неизвестный фактор: {sorted(unknown)}")

    return ScoreResult(score=total, decision=decide(total, thresholds),
                       factors=factors)
```

- [ ] **Step 4: Запустить тесты — должны пройти**

Run: `PYTHONUTF8=1 python -m pytest tests/test_scoring.py -v`
Expected: PASS, 16 тестов (включая 9 параметризованных границ)

- [ ] **Step 5: Коммит**

```bash
git add monitoring/scoring.py tests/test_scoring.py
git commit -m "feat: матрица оценки как чистая функция с эталонными случаями"
```

---

## Task 3: Стоп-правила

**Files:**
- Create: `monitoring/stop_rules.py`
- Test: `tests/test_stop_rules.py`

**Interfaces:**
- Consumes: `SourceItem` из Task 1, `Config.stop_rule_codes()`
- Produces: `check(item: SourceItem, cfg: Config) -> StopVerdict`

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_stop_rules.py
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from monitoring.config import load_config
from monitoring.models import SourceItem
from monitoring.stop_rules import check

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT)
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def item(title="Заголовок", body="Текст материала достаточной длины для проверки.",
         published=None, url="https://example.invalid/a", tier="T3"):
    return SourceItem(
        source_key="src_test", url=url, url_hash="h", title=title, body=body,
        discovered_at=NOW, published_at=published or NOW, tier=tier)


@pytest.mark.parametrize("title,body,expected", [
    ("Топ-10 лучших термосов на осень", "Подборка товаров.", "STOP_PRODUCT_ROUNDUP"),
    ("Скидки до 70% на распродаже", "Успейте купить.", "STOP_DISCOUNT_NOISE"),
    ("Наш сервис аналитики — попробуйте бесплатно", "Регистрируйтесь по ссылке.", "STOP_SERVICE_AD"),
    ("Говорят, WB поднимет комиссию", "По слухам, без источника.", "STOP_UNCONFIRMED_RUMOR"),
    ("Верь в себя и всё получится", "Мотивация для селлеров.", "STOP_MOTIVATIONAL"),
])
def test_rules_fire_with_expected_code(title, body, expected):
    assert check(item(title=title, body=body), CFG).code == expected


def test_old_material_is_stopped():
    old = NOW - timedelta(days=45)
    assert check(item(published=old), CFG).code == "STOP_OLD_NEWS"


def test_too_general_is_stopped():
    assert check(item(body="Коротко."), CFG).code == "STOP_TOO_GENERAL"


def test_useful_material_passes():
    v = check(item(
        title="Wildberries меняет тариф хранения с 3 сентября",
        body="Коэффициент хранения для категории одежда повышается с 0,7 до 1,1. "
             "Изменение затрагивает все поставки по схеме FBO и вступает в силу "
             "3 сентября 2026 года согласно обновлённой тарифной таблице."), CFG)
    assert not v.stopped
    assert v.code is None


def test_every_returned_code_exists_in_config():
    """Правило не может вернуть код, которого нет в конфиге."""
    codes = CFG.stop_rule_codes()
    samples = [
        item(title="Топ-10 товаров", body="Подборка."),
        item(title="Скидки 70%", body="Распродажа."),
        item(published=NOW - timedelta(days=45)),
        item(body="Коротко."),
    ]
    for sample in samples:
        v = check(sample, CFG)
        if v.stopped:
            assert v.code in codes, f"{v.code} нет в config/triage.yaml"
```

- [ ] **Step 2: Запустить тест и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_stop_rules.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitoring.stop_rules'`

- [ ] **Step 3: Написать минимальную реализацию**

```python
# monitoring/stop_rules.py
"""Детерминированный отсев до подсчёта баллов.

Порядок принципиален: если считать баллы первыми, рекламный материал со
штрафом −60 может добрать сумму важностью темы. См. docs/01 §4.

Граница со штрафом no_confirmation: стоп-правило STOP_UNCONFIRMED_RUMOR
срабатывает, когда источник не назван вообще и проверять нечего. Если
источник назван, но подтверждений пока нет — это фактор −50, и материал
живёт в BACKLOG до появления подтверждения. См. docs/00 §5.1.
"""
import re
from datetime import datetime, timedelta, timezone

from monitoring.models import SourceItem, StopVerdict

MAX_AGE_DAYS = 30
MIN_BODY_CHARS = 200

_ROUNDUP = re.compile(r"\bтоп[- ]?\d+|подборк|лучших?\s+\d+", re.I)
_DISCOUNT = re.compile(r"скидк\w*\s+до\s+\d+\s*%|распродаж", re.I)
_SERVICE_AD = re.compile(
    r"наш сервис|попробуйте бесплатно|регистрируйтесь|промокод|реферальн", re.I)
_RUMOUR = re.compile(r"говорят,|по слухам|ходят слухи|неподтверждённ", re.I)
_MOTIVATIONAL = re.compile(r"верь в себя|мотивац|путь к успеху|просто начни", re.I)


def check(item: SourceItem, cfg) -> StopVerdict:
    text = f"{item.title}\n{item.body}"

    if _SERVICE_AD.search(text):
        return StopVerdict("STOP_SERVICE_AD", "признаки рекламы сервиса")
    if _ROUNDUP.search(text):
        return StopVerdict("STOP_PRODUCT_ROUNDUP", "подборка товаров")
    if _DISCOUNT.search(text):
        return StopVerdict("STOP_DISCOUNT_NOISE", "скидки без рыночного значения")
    if _RUMOUR.search(text):
        return StopVerdict("STOP_UNCONFIRMED_RUMOR", "источник не назван")
    if _MOTIVATIONAL.search(text):
        return StopVerdict("STOP_MOTIVATIONAL", "мотивационный текст без фактов")

    if item.published_at is not None:
        age = item.discovered_at - item.published_at
        if age > timedelta(days=MAX_AGE_DAYS):
            return StopVerdict("STOP_OLD_NEWS", f"возраст {age.days} дней")

    if len(item.body) < MIN_BODY_CHARS:
        return StopVerdict("STOP_TOO_GENERAL", f"тело {len(item.body)} символов")

    return StopVerdict()
```

- [ ] **Step 4: Запустить тесты — должны пройти**

Run: `PYTHONUTF8=1 python -m pytest tests/test_stop_rules.py -v`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add monitoring/stop_rules.py tests/test_stop_rules.py
git commit -m "feat: стоп-правила с отсевом до подсчёта баллов"
```

---

## Task 4: Идемпотентная миграция и доступ к базе

**Files:**
- Modify: `sql/001_monitoring_map.sql` (все `CREATE TYPE` → `DO`-блоки, таблицы и индексы → `IF NOT EXISTS` с явными именами)
- Modify: `tools/check_sql.py` (добавить проверку идемпотентности)
- Create: `monitoring/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `connect(dsn: str)` — контекстный менеджер соединения; `apply_migration(conn, sql_path: Path) -> None`; `Repo` с методами `start_run(cadence) -> str`, `finish_run(run_id, *, status, fetched, stopped, scored, model_calls) -> None`, `is_known(url_hash) -> bool`, `record_stop(item, verdict, run_id) -> None`, `save_hit(item, result, run_id) -> str`

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_db.py
"""Тесты базы идут только при заданном TEST_DATABASE_URL.

Без него пропускаются: локальной Postgres на машине разработки нет,
и падающий по этой причине прогон скрывал бы настоящие ошибки.
"""
import os
from pathlib import Path
import pytest

from monitoring.db import connect, apply_migration

ROOT = Path(__file__).resolve().parents[1]
DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL не задан")


def test_migration_applies_twice_without_error():
    """Сервис перезапускается, и миграция обязана переживать повтор."""
    with connect(DSN) as conn:
        apply_migration(conn, ROOT / "sql" / "001_monitoring_map.sql")
        apply_migration(conn, ROOT / "sql" / "001_monitoring_map.sql")
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM monitoring_hits")
            assert cur.fetchone()[0] >= 0
```

- [ ] **Step 2: Запустить и убедиться, что падает или пропускается**

Run: `PYTHONUTF8=1 python -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitoring.db'` (модуль импортируется до пропуска)

- [ ] **Step 3: Переписать миграцию идемпотентно**

Все шесть `CREATE TYPE` оборачиваются в `DO`-блоки. Значения не менять — они сверяются проверялками с YAML и JSON-схемами:

```sql
DO $$ BEGIN
    CREATE TYPE monitoring_platform AS ENUM (
      'WILDBERRIES','OZON','YANDEX_MARKET','MEGAMARKET','AVITO','LAMODA',
      'ALIEXPRESS_RU','KUPER','DETSKIY_MIR','LEMANA_PRO','HOFF','VSEINSTRUMENTI',
      'CITILINK','MVIDEO','DNS','NICHE','CROSS_PLATFORM','OTHER');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE monitoring_category AS ENUM (
      'COMMISSION_TARIFF','PENALTY_DEDUCTION','OFFER_RULES','LOGISTICS','RANKING_ALGO',
      'ADVERTISING','PAYMENTS_SETTLEMENT','TAXES','REGULATION_LAW','ANTITRUST_FAS',
      'COURT_PRACTICE','PLATFORM_TOOLS','AI_TECH','MARKET_TREND','SELLER_CASE',
      'INCIDENT_OUTAGE','BUYER_IMPACT','BRAND_IP');
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
      'registry_watch','api_changelog_diff','editorial_pick');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
```

Все девять `CREATE TABLE` → `CREATE TABLE IF NOT EXISTS`.

Все девятнадцать индексов получают явные имена, иначе `IF NOT EXISTS` неприменим:

```sql
CREATE INDEX IF NOT EXISTS idx_hits_decision_score
    ON monitoring_hits (decision, score DESC) WHERE state <> 'DROPPED';
CREATE UNIQUE INDEX IF NOT EXISTS idx_hits_url_hash
    ON monitoring_hits (url_hash);
```

- [ ] **Step 4: Написать `monitoring/db.py`**

```python
# monitoring/db.py
"""Доступ к Postgres: соединение, идемпотентная миграция, репозитории."""
import io
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg


@contextmanager
def connect(dsn: str):
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_migration(conn, sql_path: Path) -> None:
    """Применяет миграцию. Безопасна при повторном вызове."""
    with io.open(sql_path, encoding="utf-8") as fh:
        sql = fh.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class Repo:
    def __init__(self, conn):
        self.conn = conn

    def start_run(self, cadence: str) -> str:
        run_id = _uid("run")
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO monitoring_runs (run_id, cadence) VALUES (%s, %s)",
                (run_id, cadence))
        self.conn.commit()
        return run_id

    def finish_run(self, run_id: str, *, status="SUCCESS", fetched=0,
                   stopped=0, scored=0, model_calls=0) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE monitoring_runs
                      SET finished_at = now(), status = %s, items_fetched = %s,
                          items_stopped = %s, items_scored = %s, model_calls = %s,
                          duration_ms = EXTRACT(MILLISECONDS FROM now() - started_at)::int
                    WHERE run_id = %s""",
                (status, fetched, stopped, scored, model_calls, run_id))
        self.conn.commit()

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
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO monitoring_hits
                       (hit_id, run_id, url, url_hash, title, excerpt,
                        discovered_at, published_at, platforms, topics,
                        categories, source_key, source_tier, score, factors,
                        decision, state)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'TRIAGED')
                   ON CONFLICT (url_hash) DO NOTHING""",
                (hit_id, run_id, item.url, item.url_hash, item.title,
                 item.body[:500], item.discovered_at, item.published_at,
                 [item.platform], list(item.topics), list(item.categories),
                 item.source_key, item.tier, result.score,
                 json.dumps(result.factors, ensure_ascii=False), result.decision))
            cur.execute(
                """INSERT INTO triage_transitions
                       (hit_id, to_decision, to_score, reason)
                   VALUES (%s, %s, %s, 'первичная оценка')""",
                (hit_id, result.decision, result.score))
        self.conn.commit()
        return hit_id
```

- [ ] **Step 5: Добавить проверку идемпотентности в `tools/check_sql.py`**

```python
# добавить после существующих проверок
double_apply_safe = (
    sql_text.count("CREATE TABLE IF NOT EXISTS") == 9
    and sql_text.count("EXCEPTION WHEN duplicate_object") == 6
    and "CREATE INDEX ON " not in sql_text
)
check("migration is safe to apply twice", double_apply_safe,
      "9 guarded tables, 6 guarded types, no unnamed indexes")
```

- [ ] **Step 6: Запустить проверялку и тесты**

Run: `PYTHONUTF8=1 python tools/check_sql.py && PYTHONUTF8=1 python -m pytest tests/ -v`
Expected: check_sql — 0 failures; тесты базы пропущены без `TEST_DATABASE_URL`

- [ ] **Step 7: Коммит**

```bash
git add sql/001_monitoring_map.sql tools/check_sql.py monitoring/db.py tests/test_db.py
git commit -m "feat: идемпотентная миграция и репозитории Postgres"
```

---

## Task 5: HTTP-клиент и нормализация

**Files:**
- Create: `monitoring/collectors/__init__.py`, `monitoring/collectors/base.py`, `monitoring/normalize.py`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Produces: `Fetcher.get(url, etag=None, last_modified=None) -> FetchResult(status, text, etag, last_modified, from_cache)`; `url_hash(url) -> str`; `make_item(source, raw: dict, now) -> SourceItem`

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_normalize.py
from datetime import datetime, timezone
from monitoring.normalize import url_hash, make_item, clean_text

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
SOURCE = {"key": "src_wb_news", "tier": "T1", "platform": "WILDBERRIES"}


def test_url_hash_ignores_tracking_params():
    a = url_hash("https://x.invalid/a?utm_source=tg&utm_medium=post")
    b = url_hash("https://x.invalid/a")
    assert a == b


def test_url_hash_is_stable_and_hex():
    h = url_hash("https://x.invalid/a")
    assert h == url_hash("https://x.invalid/a")
    assert len(h) == 64


def test_clean_text_strips_markup_and_whitespace():
    assert clean_text("<p>Текст&nbsp;тут</p>\n\n\n<b>жирный</b>") == "Текст тут жирный"


def test_make_item_carries_source_metadata():
    item = make_item(SOURCE, {"url": "https://x.invalid/a", "title": "Заголовок",
                              "body": "Тело", "published_at": NOW}, NOW)
    assert item.source_key == "src_wb_news"
    assert item.tier == "T1"
    assert item.platform == "WILDBERRIES"
    assert item.discovered_at == NOW
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_normalize.py -v`
Expected: FAIL — модуль не найден

- [ ] **Step 3: Реализация**

```python
# monitoring/normalize.py
"""Приведение любого источника к единому SourceItem."""
import hashlib
import re
from datetime import datetime
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from bs4 import BeautifulSoup

from monitoring.models import SourceItem

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
             "utm_content", "yclid", "gclid", "fbclid", "from"}


def url_hash(url: str) -> str:
    """Отпечаток адреса без меток трекинга.

    Один материал приходит из нескольких источников с разными utm-хвостами.
    Без очистки он выглядит как несколько разных находок.
    """
    p = urlparse(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query)
                       if k.lower() not in _TRACKING])
    normalized = urlunparse((p.scheme.lower(), p.netloc.lower(),
                             p.path.rstrip("/"), "", query, ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def clean_text(html: str) -> str:
    text = BeautifulSoup(html or "", "lxml").get_text(" ")
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def make_item(source: dict, raw: dict, now: datetime) -> SourceItem:
    return SourceItem(
        source_key=source["key"],
        url=raw["url"],
        url_hash=url_hash(raw["url"]),
        title=clean_text(raw.get("title", "")),
        body=clean_text(raw.get("body", "")),
        discovered_at=now,
        published_at=raw.get("published_at"),
        tier=source.get("tier", "T3"),
        platform=source.get("platform", "CROSS_PLATFORM"),
        signal=raw.get("signal"),
    )
```

```python
# monitoring/collectors/base.py
"""HTTP-клиент коллекторов: вежливый, кеширующий, с повторами."""
import time
from dataclasses import dataclass

import httpx

USER_AGENT = ("MonitoringMap/1.0 (+mailto:davidkonkin299@gmail.com) "
              "seller-news monitoring")
TIMEOUT = 20.0
RETRIES = 3


@dataclass(frozen=True)
class FetchResult:
    status: int
    text: str = ""
    etag: str | None = None
    last_modified: str | None = None
    from_cache: bool = False


class Fetcher:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
            follow_redirects=True)

    def get(self, url: str, etag=None, last_modified=None) -> FetchResult:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        delay = 2.0
        for attempt in range(RETRIES):
            try:
                r = self._client.get(url, headers=headers)
            except httpx.HTTPError:
                if attempt == RETRIES - 1:
                    raise
                time.sleep(delay)
                delay *= 2
                continue

            if r.status_code == 304:
                return FetchResult(304, from_cache=True)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == RETRIES - 1:
                    return FetchResult(r.status_code)
                wait = float(r.headers.get("Retry-After", delay))
                time.sleep(wait)
                delay *= 2
                continue
            return FetchResult(r.status_code, r.text,
                               r.headers.get("ETag"),
                               r.headers.get("Last-Modified"))
        return FetchResult(0)
```

```python
# monitoring/collectors/__init__.py
```

- [ ] **Step 4: Запустить тесты**

Run: `PYTHONUTF8=1 python -m pytest tests/test_normalize.py -v`
Expected: PASS, 4 теста

- [ ] **Step 5: Коммит**

```bash
git add monitoring/normalize.py monitoring/collectors/ tests/test_normalize.py
git commit -m "feat: HTTP-клиент коллекторов и нормализация элементов"
```

---

## Task 6: Коллектор RSS

**Files:**
- Create: `monitoring/collectors/rss.py`
- Test: `tests/test_rss.py`, `tests/fixtures/sample_feed.xml`

**Interfaces:**
- Consumes: `Fetcher`, `make_item` из Task 5
- Produces: `collect(source: dict, fetcher, now) -> list[SourceItem]`

- [ ] **Step 1: Написать падающий тест и фикстуру**

```xml
<!-- tests/fixtures/sample_feed.xml -->
<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <title>Тестовая лента</title>
  <item>
    <title>Wildberries меняет тариф хранения</title>
    <link>https://example.invalid/wb-tariff?utm_source=rss</link>
    <description>Коэффициент повышается с 3 сентября.</description>
    <pubDate>Wed, 26 Aug 2026 10:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Материал без даты</title>
    <link>https://example.invalid/no-date</link>
    <description>Тело без pubDate.</description>
  </item>
</channel></rss>
```

```python
# tests/test_rss.py
from datetime import datetime, timezone
from pathlib import Path
from monitoring.collectors.base import FetchResult
from monitoring.collectors.rss import collect

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
FEED = (Path(__file__).parent / "fixtures" / "sample_feed.xml").read_text(encoding="utf-8")
SOURCE = {"key": "src_media", "tier": "T3", "platform": "CROSS_PLATFORM",
          "url": "https://example.invalid/feed.xml"}


class FakeFetcher:
    def __init__(self, result):
        self.result = result
    def get(self, url, etag=None, last_modified=None):
        return self.result


def test_parses_entries_and_dates():
    items = collect(SOURCE, FakeFetcher(FetchResult(200, FEED)), NOW)
    assert len(items) == 2
    first = items[0]
    assert first.title == "Wildberries меняет тариф хранения"
    assert first.published_at.year == 2026
    assert first.published_at.month == 8

def test_strips_tracking_from_link():
    items = collect(SOURCE, FakeFetcher(FetchResult(200, FEED)), NOW)
    plain = collect({**SOURCE}, FakeFetcher(FetchResult(200,
        FEED.replace("?utm_source=rss", ""))), NOW)
    assert items[0].url_hash == plain[0].url_hash

def test_entry_without_date_keeps_none():
    items = collect(SOURCE, FakeFetcher(FetchResult(200, FEED)), NOW)
    assert items[1].published_at is None

def test_not_modified_returns_nothing():
    assert collect(SOURCE, FakeFetcher(FetchResult(304, from_cache=True)), NOW) == []
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_rss.py -v`
Expected: FAIL — модуль не найден

- [ ] **Step 3: Реализация**

```python
# monitoring/collectors/rss.py
"""Сбор из RSS/Atom-лент."""
import calendar
from datetime import datetime, timezone

import feedparser

from monitoring.normalize import make_item


def _published(entry):
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return None


def collect(source: dict, fetcher, now: datetime) -> list:
    result = fetcher.get(source["url"], source.get("etag"),
                         source.get("last_modified"))
    if result.status != 200 or not result.text:
        return []

    feed = feedparser.parse(result.text)
    items = []
    for entry in feed.entries:
        url = getattr(entry, "link", "")
        title = getattr(entry, "title", "")
        if not url or not title:
            continue
        body = getattr(entry, "summary", "") or getattr(entry, "description", "")
        items.append(make_item(source, {
            "url": url, "title": title, "body": body,
            "published_at": _published(entry),
        }, now))
    return items
```

- [ ] **Step 4: Запустить тесты**

Run: `PYTHONUTF8=1 python -m pytest tests/test_rss.py -v`
Expected: PASS, 4 теста

- [ ] **Step 5: Коммит**

```bash
git add monitoring/collectors/rss.py tests/test_rss.py tests/fixtures/
git commit -m "feat: коллектор RSS"
```

---

## Task 7: Коллектор сравнения снапшотов

Самый ценный коллектор карты и самый шумный. Тест на шум здесь важнее теста на срабатывание.

**Files:**
- Create: `monitoring/collectors/doc_diff.py`
- Test: `tests/test_doc_diff.py`

**Interfaces:**
- Consumes: `Fetcher` из Task 5
- Produces: `normalize_dom(html: str) -> str`; `content_hash(html: str) -> str`; `collect(source, fetcher, store, now) -> list[SourceItem]`; `SnapshotStore` с `load(key) -> str|None` и `save(key, text) -> None`

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_doc_diff.py
from datetime import datetime, timezone
from monitoring.collectors.base import FetchResult
from monitoring.collectors.doc_diff import (
    normalize_dom, content_hash, collect, SnapshotStore)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
SOURCE = {"key": "src_wb_tariffs", "tier": "T1", "platform": "WILDBERRIES",
          "url": "https://example.invalid/tariffs", "signal": "doc_change"}

PAGE_V1 = """<html><body>
  <nav>Меню</nav>
  <main><h1>Тарифы</h1><p>Коэффициент хранения: 0,7</p></main>
  <span class="views">Просмотров: 1024</span>
  <footer>Сформировано 27.08.2026 11:00</footer>
</body></html>"""

# Тот же смысл, изменился только шум: счётчик и время рендера
PAGE_NOISE = """<html><body>
  <nav>Меню</nav>
  <main><h1>Тарифы</h1><p>Коэффициент хранения: 0,7</p></main>
  <span class="views">Просмотров: 2048</span>
  <footer>Сформировано 27.08.2026 12:30</footer>
</body></html>"""

# Настоящее изменение
PAGE_V2 = PAGE_V1.replace("0,7", "1,1")


class FakeFetcher:
    def __init__(self, html):
        self.html = html
    def get(self, url, etag=None, last_modified=None):
        return FetchResult(200, self.html)


class MemStore(SnapshotStore):
    def __init__(self):
        self.data = {}
    def load(self, key):
        return self.data.get(key)
    def save(self, key, text):
        self.data[key] = text


def test_noise_does_not_change_the_hash():
    """Главный тест коллектора: счётчики и даты рендера — не изменение."""
    assert content_hash(PAGE_V1) == content_hash(PAGE_NOISE)


def test_real_change_changes_the_hash():
    assert content_hash(PAGE_V1) != content_hash(PAGE_V2)


def test_normalize_drops_chrome_and_keeps_content():
    text = normalize_dom(PAGE_V1)
    assert "Коэффициент хранения: 0,7" in text
    assert "Просмотров" not in text
    assert "Меню" not in text


def test_first_run_stores_snapshot_and_emits_nothing():
    """Первый проход не с чем сравнивать — находки быть не должно."""
    store = MemStore()
    assert collect(SOURCE, FakeFetcher(PAGE_V1), store, NOW) == []
    assert store.load("src_wb_tariffs") is not None


def test_noise_on_second_run_emits_nothing():
    store = MemStore()
    collect(SOURCE, FakeFetcher(PAGE_V1), store, NOW)
    assert collect(SOURCE, FakeFetcher(PAGE_NOISE), store, NOW) == []


def test_real_change_emits_one_item_with_diff_in_body():
    store = MemStore()
    collect(SOURCE, FakeFetcher(PAGE_V1), store, NOW)
    items = collect(SOURCE, FakeFetcher(PAGE_V2), store, NOW)
    assert len(items) == 1
    assert items[0].signal == "doc_change"
    assert items[0].platform == "WILDBERRIES"
    assert "1,1" in items[0].body
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_doc_diff.py -v`
Expected: FAIL — модуль не найден

- [ ] **Step 3: Реализация**

```python
# monitoring/collectors/doc_diff.py
"""Обнаружение тихих правок документов сравнением снапшотов.

Самый ценный сигнал карты: правку оферты или тарифа не сопровождает ни одна
публикация, и узнать о ней можно только сравнив состояние.

Главная опасность — шум. Площадки правят вёрстку, крутят счётчики просмотров
и пишут время генерации страницы. Без нормализации коллектор кричит на каждый
опрос. Поэтому normalize_dom вырезает служебное и хеш считается от смысла.
"""
import difflib
import hashlib
import io
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from monitoring.normalize import make_item

_DROP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "noscript")
_DROP_PATTERNS = re.compile(
    r"просмотр\w*\s*:?\s*\d+|сформировано\s+[\d.: ]+|обновлено\s+[\d.: ]+"
    r"|\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}", re.I)


class SnapshotStore:
    """Снапшоты страниц. В Амвере лежат на /data, иначе doc_diff не с чем сравнивать."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, key: str):
        path = self.root / f"{key}.txt"
        if not path.exists():
            return None
        with io.open(path, encoding="utf-8") as fh:
            return fh.read()

    def save(self, key: str, text: str) -> None:
        with io.open(self.root / f"{key}.txt", "w", encoding="utf-8") as fh:
            fh.write(text)


def normalize_dom(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()
    for node in soup.select(".views, .counter, .timestamp, .updated"):
        node.decompose()
    text = soup.get_text(" ")
    text = _DROP_PATTERNS.sub(" ", text)
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def content_hash(html: str) -> str:
    return hashlib.sha256(normalize_dom(html).encode("utf-8")).hexdigest()


def collect(source: dict, fetcher, store, now: datetime) -> list:
    result = fetcher.get(source["url"], source.get("etag"),
                         source.get("last_modified"))
    if result.status != 200 or not result.text:
        return []

    fresh = normalize_dom(result.text)
    previous = store.load(source["key"])
    store.save(source["key"], fresh)

    if previous is None or previous == fresh:
        return []

    diff = [line for line in difflib.unified_diff(
        previous.split(". "), fresh.split(". "), lineterm="", n=1)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
    if not diff:
        return []

    body = " ".join(diff)[:4000]
    return [make_item(source, {
        "url": source["url"],
        "title": f"Изменение документа: {source.get('title', source['key'])}",
        "body": body,
        "published_at": now,
        "signal": source.get("signal", "doc_change"),
    }, now)]
```

- [ ] **Step 4: Запустить тесты**

Run: `PYTHONUTF8=1 python -m pytest tests/test_doc_diff.py -v`
Expected: PASS, 6 тестов

- [ ] **Step 5: Коммит**

```bash
git add monitoring/collectors/doc_diff.py tests/test_doc_diff.py
git commit -m "feat: коллектор сравнения снапшотов с защитой от шума вёрстки"
```

---

## Task 8: Реестр источников и онбординг

**Files:**
- Create: `config/sources.yaml`, `monitoring/collectors/onboarding.py`
- Modify: `tools/validate.py` (проверка нового конфига)
- Test: `tests/test_onboarding.py`

**Interfaces:**
- Produces: `validate_source(source, fetcher, now) -> OnboardingReport(ok, checks, reason)`

- [ ] **Step 1: Написать `config/sources.yaml`**

Стартовые кандидаты. Адреса — гипотеза, проверяются онбордингом; не прошедшие получают `needs_review` и попадают в дайджест.

```yaml
version: 1
updated: "2026-08-27"

sources:
  # --- T1: официальные страницы площадок, сравнение снапшотов ---
  - key: src_wb_seller_news
    title: "Wildberries — новости для продавцов"
    tier: T1
    method: doc_diff
    platform: WILDBERRIES
    cadence: A
    url: "https://seller.wildberries.ru/news"
    signal: doc_change
    status: needs_review

  - key: src_wb_tariffs
    title: "Wildberries — тарифы и комиссии"
    tier: T1
    method: doc_diff
    platform: WILDBERRIES
    cadence: C
    url: "https://seller.wildberries.ru/tariffs"
    signal: doc_change
    status: needs_review

  - key: src_ozon_seller_news
    title: "Ozon — новости для продавцов"
    tier: T1
    method: doc_diff
    platform: OZON
    cadence: B
    url: "https://seller.ozon.ru/news"
    signal: doc_change
    status: needs_review

  - key: src_ym_partner_news
    title: "Яндекс Маркет — новости для партнёров"
    tier: T1
    method: doc_diff
    platform: YANDEX_MARKET
    cadence: B
    url: "https://yandex.ru/support/marketplace/news.html"
    signal: doc_change
    status: needs_review

  # --- T3: отраслевые СМИ, RSS. Дают ежедневный объём для калибровки порогов ---
  - key: src_retail_ru
    title: "Retail.ru"
    tier: T3
    method: rss
    platform: CROSS_PLATFORM
    cadence: B
    url: "https://www.retail.ru/rss/news/"
    status: needs_review

  - key: src_new_retail
    title: "New Retail"
    tier: T3
    method: rss
    platform: CROSS_PLATFORM
    cadence: B
    url: "https://new-retail.ru/rss/"
    status: needs_review

  - key: src_epepper
    title: "E-pepper"
    tier: T3
    method: rss
    platform: CROSS_PLATFORM
    cadence: B
    url: "https://e-pepper.ru/rss/"
    status: needs_review

  - key: src_vc_ecommerce
    title: "VC.ru — e-commerce"
    tier: T3
    method: rss
    platform: CROSS_PLATFORM
    cadence: B
    url: "https://vc.ru/rss/all"
    status: needs_review

# Порог онбординга (docs/01-sources соседнего проекта, §6)
onboarding:
  min_successful_requests: 3
  min_items_parsed: 3
  min_dated_share: 0.8
  min_relevant_share: 0.02
```

- [ ] **Step 2: Написать падающий тест**

```python
# tests/test_onboarding.py
from datetime import datetime, timezone
from monitoring.collectors.base import FetchResult
from monitoring.collectors.onboarding import validate_source

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
GOOD_FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Wildberries меняет комиссию</title><link>https://x.invalid/1</link>
<description>Текст</description><pubDate>Wed, 26 Aug 2026 10:00:00 +0000</pubDate></item>
<item><title>Ozon вводит тариф</title><link>https://x.invalid/2</link>
<description>Текст</description><pubDate>Wed, 26 Aug 2026 11:00:00 +0000</pubDate></item>
<item><title>Логистика маркетплейсов</title><link>https://x.invalid/3</link>
<description>Текст</description><pubDate>Wed, 26 Aug 2026 12:00:00 +0000</pubDate></item>
</channel></rss>"""

NO_DATES = GOOD_FEED.replace("<pubDate>Wed, 26 Aug 2026 10:00:00 +0000</pubDate>", "") \
                    .replace("<pubDate>Wed, 26 Aug 2026 11:00:00 +0000</pubDate>", "") \
                    .replace("<pubDate>Wed, 26 Aug 2026 12:00:00 +0000</pubDate>", "")

CFG = {"min_successful_requests": 1, "min_items_parsed": 3,
       "min_dated_share": 0.8, "min_relevant_share": 0.02}


class FakeFetcher:
    def __init__(self, result):
        self.result = result
    def get(self, url, etag=None, last_modified=None):
        return self.result


def test_good_feed_passes():
    src = {"key": "s", "method": "rss", "url": "u", "tier": "T3",
           "platform": "CROSS_PLATFORM"}
    report = validate_source(src, FakeFetcher(FetchResult(200, GOOD_FEED)), NOW, CFG)
    assert report.ok


def test_feed_without_dates_is_rejected():
    """Источник без дат ломает свежесть — допускать нельзя."""
    src = {"key": "s", "method": "rss", "url": "u", "tier": "T3",
           "platform": "CROSS_PLATFORM"}
    report = validate_source(src, FakeFetcher(FetchResult(200, NO_DATES)), NOW, CFG)
    assert not report.ok
    assert "дат" in report.reason


def test_unreachable_source_is_rejected():
    src = {"key": "s", "method": "rss", "url": "u", "tier": "T3",
           "platform": "CROSS_PLATFORM"}
    report = validate_source(src, FakeFetcher(FetchResult(404)), NOW, CFG)
    assert not report.ok
```

- [ ] **Step 3: Запустить и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_onboarding.py -v`
Expected: FAIL — модуль не найден

- [ ] **Step 4: Реализация**

```python
# monitoring/collectors/onboarding.py
"""Проверка источника перед вводом в строй.

Источник без корректных дат ломает свежесть, а источник без релевантности
засоряет поток. Не прошедший проверку получает needs_review и попадает в
дайджест — молча деградировать он не может.
"""
from dataclasses import dataclass, field
from datetime import datetime

from monitoring.collectors import rss


@dataclass(frozen=True)
class OnboardingReport:
    ok: bool
    reason: str = ""
    checks: dict = field(default_factory=dict)


def validate_source(source: dict, fetcher, now: datetime, cfg: dict) -> OnboardingReport:
    result = fetcher.get(source["url"])
    if result.status != 200:
        return OnboardingReport(False, f"источник недоступен: HTTP {result.status}")

    if source["method"] != "rss":
        # doc_diff проверяется иначе: достаточно, чтобы страница отдавала текст
        return OnboardingReport(bool(result.text.strip()),
                                "" if result.text.strip() else "пустая страница")

    items = rss.collect(source, fetcher, now)
    checks = {"items": len(items)}
    if len(items) < cfg["min_items_parsed"]:
        return OnboardingReport(False, f"разобрано элементов: {len(items)}", checks)

    dated = sum(1 for i in items if i.published_at is not None)
    share = dated / len(items)
    checks["dated_share"] = round(share, 2)
    if share < cfg["min_dated_share"]:
        return OnboardingReport(
            False, f"доля элементов с датами {share:.0%} ниже порога", checks)

    return OnboardingReport(True, "", checks)
```

- [ ] **Step 5: Добавить проверку `sources.yaml` в `tools/validate.py`**

```python
# в конец раздела 3, после проверок таксономии
try:
    sources_cfg = load_yaml("config", "sources.yaml")
    srcs = sources_cfg["sources"]
    check("config/sources.yaml parses", True, "%d sources" % len(srcs))
    keys = [s["key"] for s in srcs]
    check("source keys unique", len(keys) == len(set(keys)))
    bad_cadence = sorted(set(s["cadence"] for s in srcs) - set(cadence))
    check("every source uses a declared cadence class", not bad_cadence,
          "unknown: %s" % bad_cadence)
    bad_plat = sorted(set(s["platform"] for s in srcs) - yaml_platforms)
    check("every source references a declared platform", not bad_plat,
          "unknown: %s" % bad_plat)
    bad_method = sorted(set(s["method"] for s in srcs) - {"rss", "doc_diff"})
    check("every source uses a supported method", not bad_method,
          "unknown: %s" % bad_method)
except Exception as exc:
    check("config/sources.yaml parses", False, str(exc))
```

- [ ] **Step 6: Запустить тесты и проверялку**

Run: `PYTHONUTF8=1 python -m pytest tests/test_onboarding.py -v && PYTHONUTF8=1 python tools/validate.py`
Expected: тесты PASS; validate.py — 0 failures

- [ ] **Step 7: Коммит**

```bash
git add config/sources.yaml monitoring/collectors/onboarding.py tools/validate.py tests/test_onboarding.py
git commit -m "feat: реестр источников v1 и онбординг-проверка"
```

---

## Task 9: Механические факторы

**Files:**
- Create: `monitoring/factors/__init__.py`, `monitoring/factors/mechanical.py`
- Test: `tests/test_mechanical.py`

**Interfaces:**
- Consumes: `SourceItem`, `Config`
- Produces: `mechanical_factors(item: SourceItem, cfg: Config, *, known_urls: set, independent_sources: int) -> dict[str, str]` — возвращает словарь `{ключ: обоснование}` для скормки в `score_item`

Проставляет семь факторов: `platform_wb`, `is_fresh`, `is_old`, `authoritative_source`, `is_repeat`, `no_confirmation`, `ai_link`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_mechanical.py
from datetime import datetime, timedelta, timezone
from pathlib import Path
from monitoring.config import load_config
from monitoring.models import SourceItem
from monitoring.factors.mechanical import mechanical_factors

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT)
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def item(**kw):
    base = dict(source_key="s", url="https://x.invalid/a", url_hash="h",
                title="Заголовок", body="Тело", discovered_at=NOW,
                published_at=NOW, tier="T3", platform="CROSS_PLATFORM")
    base.update(kw)
    return SourceItem(**base)


def test_wb_platform_fires():
    f = mechanical_factors(item(platform="WILDBERRIES"), CFG,
                           known_urls=set(), independent_sources=1)
    assert "platform_wb" in f and f["platform_wb"]

def test_ozon_does_not_fire_wb_factor():
    f = mechanical_factors(item(platform="OZON"), CFG,
                           known_urls=set(), independent_sources=1)
    assert "platform_wb" not in f

def test_t1_source_is_authoritative():
    f = mechanical_factors(item(tier="T1"), CFG, known_urls=set(),
                           independent_sources=1)
    assert "authoritative_source" in f
    assert "no_confirmation" not in f

def test_t5_single_source_gets_no_confirmation_penalty():
    f = mechanical_factors(item(tier="T5"), CFG, known_urls=set(),
                           independent_sources=1)
    assert "no_confirmation" in f
    assert "authoritative_source" not in f

def test_two_independent_sources_remove_the_penalty():
    f = mechanical_factors(item(tier="T5"), CFG, known_urls=set(),
                           independent_sources=2)
    assert "no_confirmation" not in f

def test_fresh_and_old_are_mutually_exclusive():
    fresh = mechanical_factors(item(published_at=NOW - timedelta(hours=2)), CFG,
                               known_urls=set(), independent_sources=1)
    old = mechanical_factors(item(published_at=NOW - timedelta(days=20)), CFG,
                             known_urls=set(), independent_sources=1)
    assert "is_fresh" in fresh and "is_old" not in fresh
    assert "is_old" in old and "is_fresh" not in old

def test_known_url_is_repeat():
    f = mechanical_factors(item(url_hash="seen"), CFG, known_urls={"seen"},
                           independent_sources=1)
    assert "is_repeat" in f

def test_ai_keywords_fire_ai_link():
    f = mechanical_factors(item(title="Нейросеть для карточек товаров"), CFG,
                           known_urls=set(), independent_sources=1)
    assert "ai_link" in f

def test_every_returned_key_is_a_real_factor():
    f = mechanical_factors(item(platform="WILDBERRIES", tier="T1"), CFG,
                           known_urls=set(), independent_sources=2)
    assert set(f) <= set(CFG.factor_weights())
    assert all(v for v in f.values()), "обоснование не может быть пустым"
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_mechanical.py -v`
Expected: FAIL — модуль не найден

- [ ] **Step 3: Реализация**

```python
# monitoring/factors/mechanical.py
"""Факторы, которые считаются кодом.

Семь из четырнадцати не требуют суждения: площадка берётся из источника,
свежесть — из дат, авторитетность — из тира, повтор — из базы. Считать их
моделью значило бы платить за то, что и так известно, и получать плавающий
результат там, где возможен точный.
"""
import re
from datetime import timedelta

FRESH_HOURS = 72
OLD_DAYS = 14
AUTHORITATIVE_TIERS = {"T1", "T2"}

_AI = re.compile(r"\bAI\b|\bИИ\b|нейросет|искусственн\w+ интеллект|GPT|LLM|"
                 r"машинн\w+ обучени", re.I)


def mechanical_factors(item, cfg, *, known_urls: set, independent_sources: int) -> dict:
    fired = {}

    if item.platform == "WILDBERRIES":
        fired["platform_wb"] = f"источник {item.source_key} относится к Wildberries"

    if item.tier in AUTHORITATIVE_TIERS:
        fired["authoritative_source"] = f"источник тира {item.tier}"
    elif independent_sources < 2:
        fired["no_confirmation"] = (
            f"тир {item.tier}, независимых подтверждений: {independent_sources}")

    if item.published_at is not None:
        age = item.discovered_at - item.published_at
        if age <= timedelta(hours=FRESH_HOURS):
            fired["is_fresh"] = f"возраст {int(age.total_seconds() // 3600)} ч"
        elif age > timedelta(days=OLD_DAYS):
            fired["is_old"] = f"возраст {age.days} дней"

    if item.url_hash in known_urls:
        fired["is_repeat"] = "материал с таким адресом уже в базе"

    if _AI.search(f"{item.title} {item.body}"):
        fired["ai_link"] = "в тексте есть признаки темы AI"

    return fired
```

```python
# monitoring/factors/__init__.py
```

- [ ] **Step 4: Запустить тесты**

Run: `PYTHONUTF8=1 python -m pytest tests/test_mechanical.py -v`
Expected: PASS, 9 тестов

- [ ] **Step 5: Коммит**

```bash
git add monitoring/factors/ tests/test_mechanical.py
git commit -m "feat: механические факторы матрицы"
```

---

## Task 10: Факторы-суждения через Claude

**Files:**
- Create: `monitoring/factors/judgment.py`
- Test: `tests/test_judgment.py`

**Interfaces:**
- Consumes: `SourceItem`
- Produces: `judgment_factors(items: list[SourceItem], client, *, batch_size=15) -> dict[str, dict[str, str]]` — `url_hash` → `{ключ_фактора: обоснование}`; `build_prompt(items) -> str`; `parse_response(text, items) -> dict`

Проставляет семь факторов: `seller_money_impact`, `rules_change`, `has_practical_takeaway`, `mass_effect`, `legal_tax_risk`, `has_conflict`, `is_advertising`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_judgment.py
import json
from datetime import datetime, timezone
import pytest
from monitoring.models import SourceItem
from monitoring.factors.judgment import (
    judgment_factors, parse_response, JUDGMENT_KEYS)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def item(h, title="Заголовок"):
    return SourceItem(source_key="s", url=f"https://x.invalid/{h}", url_hash=h,
                      title=title, body="Тело", discovered_at=NOW, published_at=NOW)


class FakeClient:
    """Заглушка Claude: весь тракт тестируется без сети и без счёта."""
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
    def complete(self, prompt: str) -> str:
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


def test_parses_factors_for_each_item():
    items = [item("a"), item("b")]
    payload = {"a": {"seller_money_impact": "растёт комиссия"},
               "b": {"is_advertising": "реклама сервиса"}}
    got = judgment_factors(items, FakeClient(payload))
    assert got["a"] == {"seller_money_impact": "растёт комиссия"}
    assert got["b"] == {"is_advertising": "реклама сервиса"}


def test_drops_keys_outside_the_judgment_set():
    """Модель не должна проставлять механические факторы — они считаются кодом."""
    payload = {"a": {"platform_wb": "WB", "mass_effect": "весь рынок"}}
    got = judgment_factors([item("a")], FakeClient(payload))
    assert got["a"] == {"mass_effect": "весь рынок"}


def test_drops_factors_without_rationale():
    payload = {"a": {"mass_effect": "", "rules_change": "меняется оферта"}}
    got = judgment_factors([item("a")], FakeClient(payload))
    assert got["a"] == {"rules_change": "меняется оферта"}


def test_batches_are_split():
    items = [item(str(i)) for i in range(32)]
    client = FakeClient({})
    judgment_factors(items, client, batch_size=15)
    assert client.calls == 3


def test_invalid_json_yields_empty_factors_not_a_crash():
    class Broken:
        def complete(self, prompt): return "не json"
    got = judgment_factors([item("a")], Broken())
    assert got == {"a": {}}


def test_judgment_keys_do_not_overlap_mechanical():
    mechanical = {"platform_wb", "is_fresh", "is_old", "authoritative_source",
                  "is_repeat", "no_confirmation", "ai_link"}
    assert JUDGMENT_KEYS & mechanical == set()
    assert len(JUDGMENT_KEYS) == 7
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_judgment.py -v`
Expected: FAIL — модуль не найден

- [ ] **Step 3: Реализация**

```python
# monitoring/factors/judgment.py
"""Факторы, требующие суждения, — через Claude пачками.

Зовётся только для того, что прошло стоп-правила, и только за семью
факторами. Остальные семь считаются кодом: платить модели за разбор дат
и тиров источников незачем.

Модель не возвращает баллы и не видит порогов. Она отвечает только на
вопрос «сработал ли фактор и почему» — сумму считает scoring.py.
"""
import json
import os

JUDGMENT_KEYS = {
    "seller_money_impact", "rules_change", "has_practical_takeaway",
    "mass_effect", "legal_tax_risk", "has_conflict", "is_advertising",
}

_INSTRUCTION = """Ты размечаешь материалы для мониторинга маркетплейсов.

Для каждого материала определи, какие из факторов применимы. Ставь фактор,
только если он действительно есть — и обязательно с коротким обоснованием.

Факторы:
- seller_money_impact — напрямую влияет на деньги селлеров
- rules_change — меняет правила, оферту, комиссии, штрафы или логистику
- has_practical_takeaway — есть конкретное действие для продавца
- mass_effect — массовый эффект для рынка
- legal_tax_risk — судебный, налоговый или регуляторный риск
- has_conflict — конфликт, скандал или проблема
- is_advertising — рекламный материал

Ответь ТОЛЬКО объектом JSON вида:
{"<id>": {"<фактор>": "<обоснование>"}}
Материал без применимых факторов получает пустой объект.

Материалы:
"""


class AnthropicClient:
    def __init__(self, model="claude-haiku-4-5-20251001"):
        import anthropic
        self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._model = model

    def complete(self, prompt: str) -> str:
        message = self._client.messages.create(
            model=self._model, max_tokens=2048,
            messages=[{"role": "user", "content": prompt}])
        return message.content[0].text


def build_prompt(items) -> str:
    lines = []
    for it in items:
        lines.append(f'--- id: {it.url_hash}\nЗаголовок: {it.title}\n'
                     f'Текст: {it.body[:1200]}')
    return _INSTRUCTION + "\n".join(lines)


def parse_response(text: str, items) -> dict:
    ids = {it.url_hash for it in items}
    out = {i: {} for i in ids}
    try:
        raw = json.loads(text.strip())
    except (ValueError, AttributeError):
        return out
    if not isinstance(raw, dict):
        return out

    for key, factors in raw.items():
        if key not in ids or not isinstance(factors, dict):
            continue
        out[key] = {
            f: why for f, why in factors.items()
            if f in JUDGMENT_KEYS and isinstance(why, str) and why.strip()
        }
    return out


def judgment_factors(items, client, *, batch_size: int = 15) -> dict:
    result = {}
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        try:
            text = client.complete(build_prompt(batch))
        except Exception:
            result.update({it.url_hash: {} for it in batch})
            continue
        result.update(parse_response(text, batch))
    return result
```

- [ ] **Step 4: Запустить тесты**

Run: `PYTHONUTF8=1 python -m pytest tests/test_judgment.py -v`
Expected: PASS, 6 тестов

- [ ] **Step 5: Коммит**

```bash
git add monitoring/factors/judgment.py tests/test_judgment.py
git commit -m "feat: факторы-суждения через Claude пачками"
```

---

## Task 11: Heartbeat

**Files:**
- Create: `monitoring/heartbeat.py`
- Test: `tests/test_heartbeat.py`

**Interfaces:**
- Produces: `build_report(state: dict, now: datetime) -> dict` — соответствует `schemas/heartbeat.schema.json`; `QUESTIONS: list[tuple[int, str, str]]` (номер, текст, класс частоты)

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_heartbeat.py
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator
from monitoring.heartbeat import build_report, QUESTIONS

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
with io.open(ROOT / "schemas" / "heartbeat.schema.json", encoding="utf-8") as fh:
    SCHEMA = json.load(fh)

STATE = {
    "last_run_at": {"A": NOW, "B": NOW, "C": NOW, "D": NOW, "E": NOW, "F": NOW},
    "hits_by_question": {1: ["hit_abc12345"]},
    "urgent_count": 1,
    "queue_count": 0,
    "model_calls": 2,
}


def test_report_validates_against_schema():
    report = build_report(STATE, NOW)
    errors = list(Draft202012Validator(SCHEMA).iter_errors(report))
    assert not errors, [e.message for e in errors]


def test_exactly_ten_questions():
    assert len(QUESTIONS) == 10
    assert len(build_report(STATE, NOW)["answers"]) == 10


def test_every_answer_carries_data_age():
    for a in build_report(STATE, NOW)["answers"]:
        assert "data_age_seconds" in a
        assert a["data_age_seconds"] >= 0


def test_stale_class_reports_real_age_not_zero():
    """Ответ «нет» без возраста неотличим от «не проверяли»."""
    stale = dict(STATE)
    stale["last_run_at"] = dict(STATE["last_run_at"])
    stale["last_run_at"]["D"] = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
    report = build_report(stale, NOW)
    q4 = next(a for a in report["answers"] if a["question_no"] == 4)
    assert q4["data_age_seconds"] == 4 * 3600


def test_question_with_hits_answers_yes_and_lists_them():
    q1 = next(a for a in build_report(STATE, NOW)["answers"]
              if a["question_no"] == 1)
    assert q1["answer"] == "yes"
    assert q1["hit_ids"] == ["hit_abc12345"]


def test_question_without_hits_answers_no_with_empty_list():
    q7 = next(a for a in build_report(STATE, NOW)["answers"]
              if a["question_no"] == 7)
    assert q7["answer"] == "no"
    assert q7["hit_ids"] == []
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_heartbeat.py -v`
Expected: FAIL — модуль не найден

- [ ] **Step 3: Реализация**

```python
# monitoring/heartbeat.py
"""Ответы на десять вопросов ТЗ с честным возрастом данных.

Возраст обязателен у каждого ответа. Ответ «нет» без указания, насколько
свежи данные, неотличим от «не проверяли», и именно так теряются события,
случившиеся в необследованном окне. См. docs/02-cadence.md §3.
"""
from datetime import datetime
from uuid import uuid4

# (номер, текст, класс частоты, от которого считается возраст)
QUESTIONS = [
    (1, "Появилось ли что-то новое по Wildberries?", "A"),
    (2, "Появилось ли что-то, что влияет на деньги селлеров?", "A"),
    (3, "Есть ли изменения правил, комиссий, штрафов, логистики, рекламы или алгоритмов?", "C"),
    (4, "Есть ли новые регуляторные или судебные риски?", "D"),
    (5, "Есть ли массовые жалобы или сбои?", "A"),
    (6, "Есть ли важные события по Ozon или Яндекс Маркету?", "B"),
    (7, "Есть ли новые AI-инструменты или AI-тренды для маркетплейсов?", "D"),
    (8, "Есть ли рыночная аналитика, которую можно превратить в полезный пост?", "F"),
    (9, "Это свежая и подтверждённая информация?", "A"),
    (10, "Достаточно ли это важно, чтобы отправить на модерацию?", "A"),
]


def build_report(state: dict, now: datetime) -> dict:
    answers = []
    for number, text, cadence in QUESTIONS:
        last = state["last_run_at"].get(cadence)
        age = int((now - last).total_seconds()) if last else 0
        hits = list(state["hits_by_question"].get(number, []))
        answers.append({
            "question_no": number,
            "question": text,
            "answer": "yes" if hits else ("no" if last else "unknown"),
            "data_age_seconds": max(age, 0),
            "hit_ids": hits,
            "note": (f"данные класса {cadence}, возраст {age // 60} мин"
                     if last else f"класс {cadence} ещё не опрашивался"),
        })

    return {
        "report_id": f"hb_{uuid4().hex[:16]}",
        "tick_at": now.isoformat().replace("+00:00", "Z"),
        "answers": answers,
        "urgent_count": state.get("urgent_count", 0),
        "queue_count": state.get("queue_count", 0),
        "model_calls": state.get("model_calls", 0),
    }
```

- [ ] **Step 4: Запустить тесты**

Run: `PYTHONUTF8=1 python -m pytest tests/test_heartbeat.py -v`
Expected: PASS, 6 тестов

- [ ] **Step 5: Коммит**

```bash
git add monitoring/heartbeat.py tests/test_heartbeat.py
git commit -m "feat: heartbeat из десяти вопросов с возрастом данных"
```

---

## Task 12: Доставка в Telegram

**Files:**
- Create: `monitoring/delivery.py`
- Test: `tests/test_delivery.py`

**Interfaces:**
- Produces: `format_urgent(hit: dict) -> str`; `format_digest(hits: list, degraded: list, report: dict) -> str`; `send(text: str, token: str, chat_id: str) -> bool`

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_delivery.py
from monitoring.delivery import format_urgent, format_digest, TELEGRAM_LIMIT

HIT = {"hit_id": "hit_a1", "title": "WB меняет тариф хранения", "score": 130,
       "decision": "URGENT", "url": "https://x.invalid/a",
       "factors": {"platform_wb": {"hit": True, "weight": 25, "why": "тарифы WB"},
                   "seller_money_impact": {"hit": True, "weight": 25, "why": "хранение"}}}
REPORT = {"urgent_count": 1, "queue_count": 2, "answers": []}


def test_urgent_shows_score_and_link():
    text = format_urgent(HIT)
    assert "130" in text
    assert "https://x.invalid/a" in text
    assert "WB меняет тариф хранения" in text


def test_urgent_lists_fired_factors_with_rationale():
    text = format_urgent(HIT)
    assert "тарифы WB" in text
    assert "хранение" in text


def test_urgent_says_it_is_not_permission_to_publish():
    """URGENT означает «проверить первым», и в сообщении это должно быть видно."""
    assert "проверить" in format_urgent(HIT).lower()


def test_digest_fits_telegram_limit():
    many = [dict(HIT, hit_id=f"hit_{i}", title=f"Материал {i} " + "х" * 200)
            for i in range(60)]
    text = format_digest(many, [], REPORT)
    assert len(text) <= TELEGRAM_LIMIT


def test_digest_reports_degraded_sources():
    text = format_digest([], ["src_wb_tariffs"], REPORT)
    assert "src_wb_tariffs" in text


def test_empty_digest_says_so_plainly():
    text = format_digest([], [], {"urgent_count": 0, "queue_count": 0, "answers": []})
    assert "нет" in text.lower()
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_delivery.py -v`
Expected: FAIL — модуль не найден

- [ ] **Step 3: Реализация**

```python
# monitoring/delivery.py
"""Доставка в Telegram: срочные находки сразу, остальное дайджестом.

Дайджест — слой отчётности, а не доставка редактору из docs/03 §6:
кнопок и решений здесь нет.
"""
import httpx

TELEGRAM_LIMIT = 4096
API = "https://api.telegram.org/bot{token}/sendMessage"


def _fired(hit: dict) -> list:
    return [(k, v["why"]) for k, v in hit.get("factors", {}).items()
            if v.get("hit") and v.get("why")]


def format_urgent(hit: dict) -> str:
    lines = [
        f"🔴 {hit['score']} баллов — {hit['decision']}",
        "",
        hit["title"],
        hit["url"],
        "",
        "Сработали факторы:",
    ]
    lines += [f"  • {key}: {why}" for key, why in _fired(hit)]
    lines += ["", "Это приоритет очереди: проверить первым, не публиковать."]
    return "\n".join(lines)[:TELEGRAM_LIMIT]


def format_digest(hits: list, degraded: list, report: dict) -> str:
    head = [f"📋 Дайджест мониторинга",
            f"Срочных: {report.get('urgent_count', 0)} · "
            f"в очереди: {report.get('queue_count', 0)}", ""]

    if not hits and not degraded:
        return "\n".join(head + ["Новых материалов выше порога нет."])

    body = []
    for hit in hits:
        line = f"• {hit['score']} — {hit['title']}\n  {hit['url']}"
        if len("\n".join(head + body + [line])) > TELEGRAM_LIMIT - 200:
            body.append(f"…и ещё {len(hits) - len(body)} материалов")
            break
        body.append(line)

    tail = []
    if degraded:
        tail = ["", "⚠️ Источники требуют внимания:"] + \
               [f"  • {s}" for s in degraded]

    return "\n".join(head + body + tail)[:TELEGRAM_LIMIT]


def send(text: str, token: str, chat_id: str) -> bool:
    try:
        r = httpx.post(API.format(token=token),
                       json={"chat_id": chat_id, "text": text,
                             "disable_web_page_preview": True},
                       timeout=20.0)
        return r.status_code == 200
    except httpx.HTTPError:
        return False
```

- [ ] **Step 4: Запустить тесты**

Run: `PYTHONUTF8=1 python -m pytest tests/test_delivery.py -v`
Expected: PASS, 6 тестов

- [ ] **Step 5: Коммит**

```bash
git add monitoring/delivery.py tests/test_delivery.py
git commit -m "feat: доставка срочных находок и дайджеста в Telegram"
```

---

## Task 13: Точка входа, расписание и деплой

**Files:**
- Create: `app.py`, `amvera.yml`, `.env.example`
- Modify: `README.md` (раздел про запуск сборщика)
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: всё предыдущее
- Produces: `due_classes(last_run: dict, now, cadence_cfg) -> list[str]`; `run_tick(deps, now) -> dict`; CLI `--once`, `--dry-run`

- [ ] **Step 1: Написать падающий тест расписания**

```python
# tests/test_scheduler.py
from datetime import datetime, timedelta, timezone
from app import due_classes

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
CADENCE = {"A": 300, "B": 900, "C": 1800, "D": 3600, "E": 14400, "F": 86400}


def test_never_run_classes_are_all_due():
    assert set(due_classes({}, NOW, CADENCE)) == set(CADENCE)


def test_only_matured_classes_are_due():
    last = {k: NOW - timedelta(seconds=310) for k in CADENCE}
    assert due_classes(last, NOW, CADENCE) == ["A"]


def test_nothing_due_right_after_a_run():
    last = {k: NOW for k in CADENCE}
    assert due_classes(last, NOW, CADENCE) == []


def test_classes_come_back_in_cadence_order():
    last = {k: NOW - timedelta(days=2) for k in CADENCE}
    assert due_classes(last, NOW, CADENCE) == ["A", "B", "C", "D", "E", "F"]
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 3: Написать `app.py`**

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Сборщик «Карты мониторинга».

Режимы:
    python app.py            постоянный цикл (для контейнера в Амвере)
    python app.py --once     один проход (для Cron Jobs)
    python app.py --dry-run  сбор без записи в базу и без отправки

Выбор между циклом и Cron Jobs откладывается до первых замеров: тик короткий,
и платить за постоянный контейнер может оказаться незачем.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from monitoring.config import load_config

ROOT = Path(__file__).resolve().parent
POLL_SECONDS = 60


def due_classes(last_run: dict, now: datetime, cadence_cfg: dict) -> list:
    """Какие классы частоты созрели к опросу."""
    due = []
    for key in sorted(cadence_cfg):
        last = last_run.get(key)
        if last is None or (now - last).total_seconds() >= cadence_cfg[key]:
            due.append(key)
    return due


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                        help="один проход и выход")
    parser.add_argument("--dry-run", action="store_true",
                        help="без записи в базу и без отправки")
    args = parser.parse_args()

    cfg = load_config(ROOT)
    cadence = {c["key"]: c["period_seconds"] for c in cfg.queries["cadence_classes"]}
    last_run = {}

    while True:
        now = datetime.now(timezone.utc)
        for cadence_class in due_classes(last_run, now, cadence):
            print(f"[tick] class={cadence_class} at={now.isoformat()}")
            # Полный проход собирается в Task 14
            last_run[cadence_class] = now
        if args.once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Запустить тесты**

Run: `PYTHONUTF8=1 python -m pytest tests/test_scheduler.py -v`
Expected: PASS, 4 теста

- [ ] **Step 5: Написать конфигурацию деплоя**

```yaml
# amvera.yml
meta:
  environment: python
  toolchain:
    name: pip
    version: 3.12
run:
  scriptName: app.py
  persistenceMount: /data
```

```
# .env.example — образец, реальные значения задаются в панели Амверы
DATABASE_URL=postgresql://user:password@host:5432/dbname
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
SNAPSHOT_DIR=/data/snapshots
```

- [ ] **Step 6: Коммит**

```bash
git add app.py amvera.yml .env.example tests/test_scheduler.py
git commit -m "feat: точка входа, расписание классов частоты и конфигурация деплоя"
```

---

## Task 14: Сборка тракта и сквозной прогон

**Files:**
- Modify: `app.py` (собрать полный проход из готовых модулей)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: все модули из Task 1–13
- Produces: `run_tick(cadence_class, deps, now) -> dict` со счётчиками `fetched`, `stopped`, `scored`, `urgent`, `model_calls`

- [ ] **Step 1: Написать падающий сквозной тест**

```python
# tests/test_pipeline.py
"""Сквозной тест тракта на заглушках: без сети, без базы, без Claude."""
from datetime import datetime, timezone
from pathlib import Path
from monitoring.config import load_config
from monitoring.collectors.base import FetchResult
from app import run_tick, Deps

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT)
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

USEFUL_FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Wildberries меняет тариф хранения с 3 сентября</title>
<link>https://x.invalid/wb</link>
<description>Коэффициент хранения для категории одежда повышается с 0,7 до 1,1.
Изменение затрагивает все поставки по схеме FBO и вступает в силу третьего
сентября согласно обновлённой тарифной таблице площадки.</description>
<pubDate>Thu, 27 Aug 2026 10:00:00 +0000</pubDate></item>
<item><title>Топ-10 лучших термосов</title><link>https://x.invalid/top</link>
<description>Подборка товаров для осени.</description>
<pubDate>Thu, 27 Aug 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""


class FakeFetcher:
    def get(self, url, etag=None, last_modified=None):
        return FetchResult(200, USEFUL_FEED)


class FakeRepo:
    def __init__(self):
        self.hits, self.stops = [], []
        self.known = set()
    def start_run(self, cadence): return "run_test"
    def finish_run(self, run_id, **kw): pass
    def is_known(self, h): return h in self.known
    def record_stop(self, item, verdict, run_id): self.stops.append((item, verdict))
    def save_hit(self, item, result, run_id):
        self.hits.append((item, result))
        self.known.add(item.url_hash)
        return f"hit_{len(self.hits):08d}"


class FakeJudge:
    def complete(self, prompt):
        return ('{"' + prompt.split("id: ")[1].split("\\n")[0] + '": '
                '{"seller_money_impact": "растёт тариф хранения", '
                '"rules_change": "новая тарифная таблица", '
                '"has_practical_takeaway": "пересчитать себестоимость", '
                '"mass_effect": "все поставки FBO"}}')


def deps():
    return Deps(cfg=CFG, fetcher=FakeFetcher(), repo=FakeRepo(),
                judge=FakeJudge(), store=None,
                sources=[{"key": "src_media", "tier": "T3", "method": "rss",
                          "platform": "WILDBERRIES", "cadence": "B",
                          "url": "https://x.invalid/feed"}])


def test_roundup_is_stopped_before_scoring():
    d = deps()
    run_tick("B", d, NOW)
    assert any(v.code == "STOP_PRODUCT_ROUNDUP" for _, v in d.repo.stops)


def test_useful_item_is_scored_and_saved():
    d = deps()
    counters = run_tick("B", d, NOW)
    assert counters["scored"] == 1
    item, result = d.repo.hits[0]
    assert "Wildberries" in item.title
    assert result.score >= 60


def test_second_tick_marks_repeat_not_duplicate_row():
    d = deps()
    run_tick("B", d, NOW)
    run_tick("B", d, NOW)
    assert len(d.repo.hits) == 1, "тот же адрес не должен создавать вторую находку"


def test_counters_are_reported():
    counters = run_tick("B", deps(), NOW)
    assert set(counters) >= {"fetched", "stopped", "scored", "urgent", "model_calls"}
    assert counters["fetched"] == 2
    assert counters["stopped"] == 1
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_pipeline.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_tick'`

- [ ] **Step 3: Собрать тракт в `app.py`**

```python
# добавить в app.py
from dataclasses import dataclass

from monitoring import stop_rules
from monitoring.collectors import rss, doc_diff
from monitoring.factors.mechanical import mechanical_factors
from monitoring.factors.judgment import judgment_factors
from monitoring.scoring import score_item


@dataclass
class Deps:
    cfg: object
    fetcher: object
    repo: object
    judge: object
    store: object
    sources: list


def run_tick(cadence_class: str, deps: Deps, now: datetime) -> dict:
    counters = {"fetched": 0, "stopped": 0, "scored": 0,
                "urgent": 0, "model_calls": 0}
    run_id = deps.repo.start_run(cadence_class)

    collected = []
    for source in deps.sources:
        if source.get("cadence") != cadence_class:
            continue
        if source["method"] == "rss":
            collected += rss.collect(source, deps.fetcher, now)
        elif source["method"] == "doc_diff":
            collected += doc_diff.collect(source, deps.fetcher, deps.store, now)
    counters["fetched"] = len(collected)

    survivors = []
    for item in collected:
        if deps.repo.is_known(item.url_hash):
            continue
        verdict = stop_rules.check(item, deps.cfg)
        if verdict.stopped:
            deps.repo.record_stop(item, verdict, run_id)
            counters["stopped"] += 1
            continue
        survivors.append(item)

    judged = {}
    if survivors and deps.judge is not None:
        judged = judgment_factors(survivors, deps.judge)
        counters["model_calls"] = (len(survivors) + 14) // 15

    weights = deps.cfg.factor_weights()
    thresholds = deps.cfg.thresholds()
    for item in survivors:
        fired = mechanical_factors(item, deps.cfg, known_urls=set(),
                                   independent_sources=1)
        fired.update(judged.get(item.url_hash, {}))
        result = score_item(fired, weights, thresholds)
        deps.repo.save_hit(item, result, run_id)
        counters["scored"] += 1
        if result.decision == "URGENT":
            counters["urgent"] += 1

    deps.repo.finish_run(run_id, fetched=counters["fetched"],
                         stopped=counters["stopped"], scored=counters["scored"],
                         model_calls=counters["model_calls"])
    return counters
```

- [ ] **Step 4: Запустить весь набор тестов**

Run: `PYTHONUTF8=1 python -m pytest tests/ -v`
Expected: PASS, все тесты

- [ ] **Step 5: Прогнать проверялки согласованности**

Run: `PYTHONUTF8=1 python tools/validate.py && PYTHONUTF8=1 python tools/check_sql.py`
Expected: 0 failures в обеих

- [ ] **Step 6: Сухой прогон на настоящих источниках**

Run: `PYTHONUTF8=1 python app.py --once --dry-run`
Expected: видно, какие источники ответили, сколько элементов пришло, сколько отсеяно и с какими баллами прошли остальные. Источники, не прошедшие онбординг, отмечены `needs_review`.

- [ ] **Step 7: Коммит**

```bash
git add app.py tests/test_pipeline.py
git commit -m "feat: сборка тракта от сбора до записи находок"
```

---

## Task 15: Тематическая разметка по запросам

Здесь 89 запросов из `config/queries.yaml` начинают работать. Сегодня — как матчеры тем и площадок; завтра те же строки уйдут в поисковый API, и этот модуль не изменится. Без него находки пишутся с пустыми `topics` и `categories`, а вся таксономия карты остаётся мёртвой.

**Files:**
- Create: `monitoring/topics.py`
- Modify: `app.py` (вызов разметки перед оценкой)
- Test: `tests/test_topics.py`

**Interfaces:**
- Consumes: `Config` из Task 1, `SourceItem`
- Produces: `build_matchers(cfg) -> list[Matcher]`; `classify(item, matchers, cfg) -> tuple[tuple[str, ...], tuple[str, ...]]` — кортежи тем и категорий

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_topics.py
from datetime import datetime, timezone
from pathlib import Path
from monitoring.config import load_config
from monitoring.models import SourceItem
from monitoring.topics import build_matchers, classify

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT)
M = build_matchers(CFG)
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def item(title, body=""):
    return SourceItem(source_key="s", url="https://x.invalid/a", url_hash="h",
                      title=title, body=body, discovered_at=NOW, published_at=NOW)


def test_all_89_queries_become_matchers():
    assert len(M) == 89


def test_commission_news_gets_money_topic():
    topics, categories = classify(
        item("Wildberries комиссии вырастут", "Комиссия по категории меняется."),
        M, CFG)
    assert "seller_money" in topics
    assert "COMMISSION_TARIFF" in categories


def test_offer_news_gets_rules_topic():
    topics, _ = classify(item("Wildberries оферта обновлена"), M, CFG)
    assert "rules_offer" in topics


def test_matching_requires_all_query_words():
    """«Wildberries» без второго слова запроса не должен давать тему комиссий."""
    topics, _ = classify(item("Wildberries открыл склад в Казани"), M, CFG)
    assert "seller_money" not in topics


def test_unmatched_item_gets_empty_tuples():
    topics, categories = classify(item("Погода в Москве на выходных"), M, CFG)
    assert topics == ()
    assert categories == ()


def test_every_returned_topic_and_category_is_declared():
    topics, categories = classify(
        item("Ozon реклама и Яндекс Маркет комиссии"), M, CFG)
    assert set(topics) <= CFG.topic_keys()
    declared = {s["category"] for t in CFG.map["topics"] for s in t["subtopics"]}
    assert set(categories) <= declared
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_topics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitoring.topics'`

- [ ] **Step 3: Реализация**

```python
# monitoring/topics.py
"""Тематическая разметка находок.

Шов под поиск: сегодня 89 запросов из config/queries.yaml работают матчерами
темы и площадки, завтра те же строки уходят в поисковый API. Интерфейс этого
модуля при переходе не меняется.

Совпадением считается присутствие ВСЕХ значимых слов запроса. «Wildberries»
в одиночку не должен давать тему комиссий — иначе разметка вырождается
в «всё про WB».
"""
import re
from collections import Counter
from dataclasses import dataclass

MIN_WORD = 3


@dataclass(frozen=True)
class Matcher:
    words: tuple
    topic: str
    platform: str


def _words(text: str) -> list:
    return [w for w in re.findall(r"\w+", text.lower()) if len(w) >= MIN_WORD]


def build_matchers(cfg) -> list:
    matchers = []
    for group in cfg.queries["query_groups"]:
        for query in group["queries"]:
            matchers.append(Matcher(tuple(_words(query["text"])),
                                    query["topic"], query["platform"]))
    return matchers


def _subtopic_categories(cfg, topic_key: str, haystack: set) -> list:
    """Категории подтем, чьи слова встретились в тексте."""
    found = []
    for topic in cfg.map["topics"]:
        if topic["key"] != topic_key:
            continue
        for sub in topic["subtopics"]:
            sub_words = set(_words(sub["title"]))
            if sub_words and sub_words <= haystack:
                found.append(sub["category"])
        if not found:
            # Тема совпала, конкретная подтема — нет. Берём преобладающую
            # категорию темы, чтобы находка не осталась без категории вовсе.
            counts = Counter(s["category"] for s in topic["subtopics"])
            found.append(counts.most_common(1)[0][0])
    return found


def classify(item, matchers: list, cfg) -> tuple:
    haystack = set(_words(f"{item.title} {item.body}"))

    topics = []
    for matcher in matchers:
        if matcher.words and set(matcher.words) <= haystack:
            if matcher.topic not in topics:
                topics.append(matcher.topic)

    if not topics:
        return (), ()

    categories = []
    for topic_key in topics:
        for category in _subtopic_categories(cfg, topic_key, haystack):
            if category not in categories:
                categories.append(category)

    return tuple(topics), tuple(categories)
```

- [ ] **Step 4: Подключить разметку в `app.py`**

В `run_tick`, сразу после отбора `survivors` и до оценки, заменить создание находки так, чтобы `SourceItem` получал темы и категории:

```python
from dataclasses import replace
from monitoring.topics import build_matchers, classify

# один раз на тик, до цикла оценки:
matchers = build_matchers(deps.cfg)

# внутри цикла по survivors, перед mechanical_factors:
    topics, categories = classify(item, matchers, deps.cfg)
    item = replace(item, topics=topics, categories=categories)
```

- [ ] **Step 5: Запустить тесты**

Run: `PYTHONUTF8=1 python -m pytest tests/test_topics.py tests/test_pipeline.py -v`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
git add monitoring/topics.py app.py tests/test_topics.py
git commit -m "feat: тематическая разметка находок по запросам карты"
```

---

## Task 16: Heartbeat, доставка и защита от наложения тиков

Замыкает контур: до этой задачи находки копятся в базе, но никто о них не узнаёт.

**Files:**
- Modify: `app.py` (вызов heartbeat и доставки, блокировка тика, подъём из BACKLOG)
- Modify: `monitoring/db.py` (методы `promote_backlog`, `pending_digest`, `degraded_sources`, `save_heartbeat`)
- Test: `tests/test_tick_integration.py`

**Interfaces:**
- Consumes: `build_report` из Task 11, `format_urgent`/`format_digest`/`send` из Task 12, `promotion_delta` из Task 2
- Produces: `Repo.promote_backlog(now, weights, thresholds) -> list[str]`; `Repo.save_heartbeat(report, run_id) -> None`; `run_heartbeat(deps, state, now) -> dict`; `_tick_lock(cadence_class) -> bool`

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_tick_integration.py
from datetime import datetime, timedelta, timezone
from pathlib import Path
from monitoring.config import load_config
from app import run_heartbeat, acquire_tick_lock, release_tick_lock, Deps

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT)
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


class SpySender:
    def __init__(self):
        self.sent = []
    def __call__(self, text, token, chat_id):
        self.sent.append(text)
        return True


class FakeRepo:
    def __init__(self, urgent=(), queued=()):
        self._urgent, self._queued = list(urgent), list(queued)
        self.heartbeats = []
    def pending_digest(self): return self._queued
    def pending_urgent(self): return self._urgent
    def degraded_sources(self): return []
    def promote_backlog(self, now, weights, thresholds): return []
    def save_heartbeat(self, report, run_id): self.heartbeats.append(report)
    def mark_delivered(self, hit_ids): pass


HIT = {"hit_id": "hit_a1", "title": "WB меняет тариф", "score": 130,
       "decision": "URGENT", "url": "https://x.invalid/a",
       "factors": {"platform_wb": {"hit": True, "weight": 25, "why": "тарифы WB"}}}


def deps(repo, sender):
    return Deps(cfg=CFG, fetcher=None, repo=repo, judge=None, store=None,
                sources=[], sender=sender, token="t", chat_id="c")


def test_urgent_is_sent_immediately():
    sender = SpySender()
    repo = FakeRepo(urgent=[HIT])
    run_heartbeat(deps(repo, sender), {"last_run_at": {"A": NOW}}, NOW)
    assert any("130" in t for t in sender.sent)


def test_empty_tick_sends_nothing():
    """Тридцать сообщений «нового нет» подряд — это не мониторинг."""
    sender = SpySender()
    run_heartbeat(deps(FakeRepo(), sender), {"last_run_at": {"A": NOW}}, NOW)
    assert sender.sent == []


def test_heartbeat_is_always_saved_even_when_empty():
    repo = FakeRepo()
    run_heartbeat(deps(repo, SpySender()), {"last_run_at": {"A": NOW}}, NOW)
    assert len(repo.heartbeats) == 1
    assert len(repo.heartbeats[0]["answers"]) == 10


def test_tick_lock_prevents_overlap():
    assert acquire_tick_lock("A") is True
    assert acquire_tick_lock("A") is False, "второй тик того же класса не должен стартовать"
    release_tick_lock("A")
    assert acquire_tick_lock("A") is True
    release_tick_lock("A")


def test_different_classes_do_not_block_each_other():
    assert acquire_tick_lock("A") is True
    assert acquire_tick_lock("B") is True
    release_tick_lock("A")
    release_tick_lock("B")
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_tick_integration.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_heartbeat'`

- [ ] **Step 3: Дописать репозиторий**

```python
# добавить в monitoring/db.py, в класс Repo
    def pending_urgent(self) -> list:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT hit_id, title, url, score, decision, factors
                     FROM monitoring_hits
                    WHERE decision = 'URGENT' AND handed_off_at IS NULL
                    ORDER BY score DESC, discovered_at""")
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def pending_digest(self) -> list:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT hit_id, title, url, score, decision, factors
                     FROM monitoring_hits
                    WHERE decision IN ('QUEUE','BACKLOG') AND handed_off_at IS NULL
                    ORDER BY score DESC, discovered_at
                    LIMIT 50""")
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def mark_delivered(self, hit_ids: list) -> None:
        if not hit_ids:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE monitoring_hits SET handed_off_at = now(), state = 'HANDED_OFF'
                    WHERE hit_id = ANY(%s)""", (hit_ids,))
        self.conn.commit()

    def degraded_sources(self) -> list:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT query_id FROM monitoring_queries
                    WHERE consecutive_failures >= 3 AND enabled""")
            return [row[0] for row in cur.fetchall()]

    def save_heartbeat(self, report: dict, run_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO heartbeat_reports
                       (report_id, run_id, answers, urgent_count, queue_count,
                        model_calls, is_empty)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (report["report_id"], run_id,
                 json.dumps(report["answers"], ensure_ascii=False),
                 report["urgent_count"], report["queue_count"],
                 report.get("model_calls", 0),
                 report["urgent_count"] == 0 and report["queue_count"] == 0))
        self.conn.commit()

    def promote_backlog(self, now, weights: dict, thresholds: list) -> list:
        """Материалы в BACKLOG, получившие подтверждение, поднимаются в полосе.

        docs/01 §5.2: снятие штрафа плюс авторитетный источник дают +65.
        Просроченные уходят в DROP с причиной EXPIRED_IN_BACKLOG.
        """
        from monitoring.scoring import decide, promotion_delta
        promoted = []
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT hit_id, score FROM monitoring_hits
                    WHERE decision = 'BACKLOG'
                      AND factors -> 'no_confirmation' ->> 'hit' = 'true'
                      AND (backlog_until IS NULL OR backlog_until >= %s)""",
                (now.date(),))
            rows = cur.fetchall()

            delta = promotion_delta(weights)
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
```

- [ ] **Step 4: Дописать `app.py`**

```python
# добавить в app.py
import threading

from monitoring.heartbeat import build_report
from monitoring.delivery import format_urgent, format_digest, send

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
DIGEST_EVERY_HOURS = 12


def acquire_tick_lock(cadence_class: str) -> bool:
    """Два тика одного класса одновременно дадут дубли находок и двойной счёт."""
    with _LOCKS_GUARD:
        if _LOCKS.get(cadence_class):
            return False
        _LOCKS[cadence_class] = True
        return True


def release_tick_lock(cadence_class: str) -> None:
    with _LOCKS_GUARD:
        _LOCKS[cadence_class] = False


def run_heartbeat(deps, state: dict, now: datetime) -> dict:
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
    for hit in urgent:
        if deps.sender(format_urgent(hit), deps.token, deps.chat_id):
            delivered.append(hit["hit_id"])

    last_digest = state.get("last_digest_at")
    digest_due = (last_digest is None or
                  (now - last_digest).total_seconds() >= DIGEST_EVERY_HOURS * 3600)
    if digest_due and (queued or degraded):
        if deps.sender(format_digest(queued, degraded, report),
                       deps.token, deps.chat_id):
            delivered += [h["hit_id"] for h in queued]
            state["last_digest_at"] = now

    deps.repo.mark_delivered(delivered)
    return report
```

Поле `Deps` дополняется тремя значениями:

```python
@dataclass
class Deps:
    cfg: object
    fetcher: object
    repo: object
    judge: object
    store: object
    sources: list
    sender: object = send
    token: str = ""
    chat_id: str = ""
```

В `main()` тик оборачивается блокировкой:

```python
        for cadence_class in due_classes(last_run, now, cadence):
            if not acquire_tick_lock(cadence_class):
                print(f"[skip] class={cadence_class} SKIPPED_OVERLAP")
                continue
            try:
                counters = run_tick(cadence_class, deps, now)
                print(f"[tick] class={cadence_class} {counters}")
            finally:
                release_tick_lock(cadence_class)
            last_run[cadence_class] = now
        run_heartbeat(deps, state, now)
```

- [ ] **Step 5: Запустить весь набор тестов**

Run: `PYTHONUTF8=1 python -m pytest tests/ -v`
Expected: PASS, все тесты

- [ ] **Step 6: Коммит**

```bash
git add app.py monitoring/db.py tests/test_tick_integration.py
git commit -m "feat: heartbeat, доставка, блокировка тика и подъём из BACKLOG"
```

---

## Проверка готовности v1

Всё должно выполняться без падений:

```bash
PYTHONUTF8=1 python -m pytest tests/ -v
PYTHONUTF8=1 python tools/validate.py
PYTHONUTF8=1 python tools/check_sql.py
PYTHONUTF8=1 python app.py --once --dry-run
```

Готовность к деплою:

1. В Амвере создан проект PostgreSQL (тариф не ниже «Начальный») и проект приложения.
2. Заданы четыре переменные окружения.
3. Код доставлен: `git push` при доступном `git.amvera.ru`, иначе через Онлайн IDE или загрузку файлов интерфейсом — см. риск R1 в спеке.
4. Миграция применилась при старте, в логах видно `[tick] class=A`.
5. В `@CMZakaz_Bot` пришёл первый дайджест.

## Что остаётся за пределами v1

Telegram-каналы как источник, госреестры, площадки P4, детектор массовости, поисковый API, фактчекинг, четыре гейта публикации, написание постов, модерация с кнопками.
