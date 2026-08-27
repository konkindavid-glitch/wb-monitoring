"""Общие типы, которые ходят между модулями."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SourceItem:
    """Элемент из любого источника, приведённый к единому виду."""

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
    """Результат оценки: сумма, полоса очереди и полный разбор по факторам."""

    score: int
    decision: str
    factors: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StopVerdict:
    """Вердикт стоп-правил. code = None означает «прошло»."""

    code: str | None = None
    detail: str | None = None

    @property
    def stopped(self) -> bool:
        return self.code is not None
