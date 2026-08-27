"""Матрица оценки: сумма баллов и полоса очереди.

Модуль намеренно чистый — никакого ввода-вывода. Вся матрица из ТЗ живёт
здесь и покрывается тестами без сети и без базы.

ВАЖНО: сумма определяет ПРИОРИТЕТ ОЧЕРЕДИ, а не разрешение на публикацию.
Взвешенная сумма допускает компенсацию: слух без подтверждения набирает
115 − 50 = 65 баллов и по шкале ТЗ попадает в рабочую полосу. Защита в том,
что QUEUE означает «проверить первым», а не «публиковать». Публикацию решают
четыре независимых гейта через AND, они живут downstream.
См. docs/01-triage-scoring.md §3.
"""
from monitoring.models import ScoreResult


def decide(score: int, thresholds: list) -> str:
    """Полоса очереди по сумме баллов."""
    for band in thresholds:
        if band["min_score"] is not None and score >= band["min_score"]:
            return band["decision"]
    return "DROP"


def promotion_delta(weights: dict) -> int:
    """Прибавка при появлении независимого подтверждения.

    Снимается штраф no_confirmation и добавляется authoritative_source.
    См. docs/01-triage-scoring.md §5.2.
    """
    return -weights["no_confirmation"] + weights["authoritative_source"]


def score_item(fired: dict, weights: dict, thresholds: list) -> ScoreResult:
    """Считает сумму и полосу.

    fired — словарь {ключ_фактора: обоснование}. Ключ отсутствует — фактор
    не сработал. Пустое обоснование — ошибка: фактор без обоснования
    не засчитывается (docs/01 §6).
    """
    unknown = set(fired) - set(weights)
    if unknown:
        raise ValueError(f"неизвестный фактор: {sorted(unknown)}")

    factors = {}
    total = 0
    for key, weight in weights.items():
        if key in fired:
            why = fired[key]
            if not why or not str(why).strip():
                raise ValueError(
                    f"у сработавшего фактора {key} должно быть обоснование")
            factors[key] = {"hit": True, "weight": weight, "why": why}
            total += weight
        else:
            factors[key] = {"hit": False, "weight": 0}

    return ScoreResult(score=total, decision=decide(total, thresholds),
                       factors=factors)
