"""Ответы на десять вопросов ТЗ с честным возрастом данных.

Возраст обязателен у каждого ответа. Ответ «нет» без указания, насколько
свежи данные, неотличим от «не проверяли», и именно так теряются события,
случившиеся в необследованном окне. См. docs/02-cadence.md §3.
"""
from datetime import datetime
from uuid import uuid4

# (номер, текст, класс частоты, от которого считается возраст ответа)
QUESTIONS = [
    (1, "Появилось ли что-то новое по Wildberries?", "A"),
    (2, "Появилось ли что-то, что влияет на деньги селлеров?", "A"),
    (3, "Есть ли изменения правил, комиссий, штрафов, логистики, рекламы "
        "или алгоритмов?", "C"),
    (4, "Есть ли новые регуляторные или судебные риски?", "D"),
    (5, "Есть ли массовые жалобы или сбои?", "A"),
    (6, "Есть ли важные события по Ozon или Яндекс Маркету?", "B"),
    (7, "Есть ли новые AI-инструменты или AI-тренды для маркетплейсов?", "D"),
    (8, "Есть ли рыночная аналитика, которую можно превратить в полезный пост?", "F"),
    (9, "Это свежая и подтверждённая информация?", "A"),
    (10, "Достаточно ли это важно, чтобы отправить на модерацию?", "A"),
]


def map_hits_to_questions(hits) -> dict:
    """Раскладывает находки тика по десяти вопросам ТЗ.

    Без этого heartbeat отвечает «нет» на всё и всегда — даже когда находки
    есть, — и вся функция становится декоративной.

    hits — последовательность (hit_id, item, result).
    """
    by_question = {number: [] for number, _, _ in QUESTIONS}

    def add(number, hit_id):
        if hit_id not in by_question[number]:
            by_question[number].append(hit_id)

    for hit_id, item, result in hits:
        fired = {k for k, v in result.factors.items() if v.get("hit")}
        categories = set(item.categories)

        if item.platform == "WILDBERRIES":
            add(1, hit_id)
        if "seller_money_impact" in fired:
            add(2, hit_id)
        if "rules_change" in fired:
            add(3, hit_id)
        if "legal_tax_risk" in fired:
            add(4, hit_id)
        if "INCIDENT_OUTAGE" in categories or "has_conflict" in fired:
            add(5, hit_id)
        if item.platform in ("OZON", "YANDEX_MARKET"):
            add(6, hit_id)
        if "ai_link" in fired:
            add(7, hit_id)
        if "MARKET_TREND" in categories:
            add(8, hit_id)
        if "is_fresh" in fired and "no_confirmation" not in fired:
            add(9, hit_id)
        if result.decision in ("URGENT", "QUEUE"):
            add(10, hit_id)

    return {number: ids for number, ids in by_question.items() if ids}


def build_report(state: dict, now: datetime) -> dict:
    """Отчёт по schemas/heartbeat.schema.json."""
    last_run = state.get("last_run_at", {})
    by_question = state.get("hits_by_question", {})

    answers = []
    for number, text, cadence in QUESTIONS:
        last = last_run.get(cadence)
        age = int((now - last).total_seconds()) if last else 0
        hits = list(by_question.get(number, []))

        if not last:
            answer, note = "unknown", f"класс {cadence} ещё не опрашивался"
        elif hits:
            answer = "yes"
            note = f"данные класса {cadence}, возраст {age // 60} мин"
        else:
            answer = "no"
            note = f"данные класса {cadence}, возраст {age // 60} мин"

        answers.append({
            "question_no": number,
            "question": text,
            "answer": answer,
            "data_age_seconds": max(age, 0),
            "hit_ids": hits,
            "note": note,
        })

    return {
        "report_id": f"hb_{uuid4().hex[:16]}",
        "tick_at": now.isoformat().replace("+00:00", "Z"),
        "answers": answers,
        "urgent_count": state.get("urgent_count", 0),
        "queue_count": state.get("queue_count", 0),
        "model_calls": state.get("model_calls", 0),
    }
