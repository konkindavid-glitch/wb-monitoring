import io
import json
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from monitoring.heartbeat import QUESTIONS, build_report

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

with io.open(ROOT / "schemas" / "heartbeat.schema.json", encoding="utf-8") as fh:
    SCHEMA = json.load(fh)

STATE = {
    "last_run_at": {c: NOW for c in "ABCDEF"},
    "hits_by_question": {1: ["hit_abc12345"]},
    "urgent_count": 1,
    "queue_count": 0,
    "model_calls": 2,
}


def test_report_validates_against_its_schema():
    errors = list(Draft202012Validator(SCHEMA).iter_errors(build_report(STATE, NOW)))
    assert not errors, [e.message for e in errors]


def test_exactly_ten_questions():
    assert len(QUESTIONS) == 10
    assert len(build_report(STATE, NOW)["answers"]) == 10


def test_question_numbers_are_one_to_ten():
    numbers = [a["question_no"] for a in build_report(STATE, NOW)["answers"]]
    assert numbers == list(range(1, 11))


def test_every_answer_carries_data_age():
    for answer in build_report(STATE, NOW)["answers"]:
        assert "data_age_seconds" in answer
        assert answer["data_age_seconds"] >= 0


def test_stale_class_reports_real_age_not_zero():
    """Ответ «нет» без возраста неотличим от «не проверяли»."""
    stale = dict(STATE)
    stale["last_run_at"] = dict(STATE["last_run_at"])
    stale["last_run_at"]["D"] = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
    report = build_report(stale, NOW)
    q4 = next(a for a in report["answers"] if a["question_no"] == 4)
    assert q4["data_age_seconds"] == 4 * 3600


def test_never_polled_class_answers_unknown_not_no():
    """«Не проверяли» — это не «нет»."""
    fresh = {"last_run_at": {}, "hits_by_question": {}}
    for answer in build_report(fresh, NOW)["answers"]:
        assert answer["answer"] == "unknown"


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


# --- раскладка находок по вопросам ----------------------------------------

from monitoring.heartbeat import map_hits_to_questions  # noqa: E402
from monitoring.models import ScoreResult, SourceItem  # noqa: E402


def hit(hit_id, *, platform="WILDBERRIES", fired=(), categories=(),
        decision="QUEUE", score=65):
    item = SourceItem(source_key="s", url=f"https://x/{hit_id}", url_hash=hit_id,
                      title="Заголовок", body="Тело", discovered_at=NOW,
                      published_at=NOW, platform=platform, categories=categories)
    factors = {name: {"hit": name in fired, "weight": 0} for name in (
        "platform_wb", "seller_money_impact", "rules_change",
        "authoritative_source", "is_fresh", "has_practical_takeaway",
        "mass_effect", "ai_link", "legal_tax_risk", "has_conflict",
        "is_repeat", "is_old", "no_confirmation", "is_advertising")}
    return (hit_id, item, ScoreResult(score=score, decision=decision,
                                      factors=factors))


def test_wb_hit_answers_question_one():
    assert map_hits_to_questions([hit("hit_a")]).get(1) == ["hit_a"]


def test_money_impact_answers_question_two():
    mapped = map_hits_to_questions([hit("hit_a", fired={"seller_money_impact"})])
    assert mapped.get(2) == ["hit_a"]


def test_ozon_answers_question_six_not_one():
    mapped = map_hits_to_questions([hit("hit_a", platform="OZON")])
    assert mapped.get(6) == ["hit_a"]
    assert 1 not in mapped


def test_incident_category_answers_question_five():
    mapped = map_hits_to_questions([hit("hit_a", categories=("INCIDENT_OUTAGE",))])
    assert mapped.get(5) == ["hit_a"]


def test_unconfirmed_item_does_not_answer_question_nine():
    """Вопрос 9 — «свежая и подтверждённая». Без подтверждения ответа нет."""
    confirmed = map_hits_to_questions([hit("hit_a", fired={"is_fresh"})])
    unconfirmed = map_hits_to_questions(
        [hit("hit_b", fired={"is_fresh", "no_confirmation"})])
    assert confirmed.get(9) == ["hit_a"]
    assert 9 not in unconfirmed


def test_dropped_item_does_not_answer_question_ten():
    mapped = map_hits_to_questions([hit("hit_a", decision="DROP", score=-10)])
    assert 10 not in mapped


def test_mapping_feeds_the_report_and_turns_answers_to_yes():
    """Главное: раскладка должна доезжать до отчёта.

    Раньше hits_by_question никто не заполнял, и heartbeat отвечал «нет»
    на все десять вопросов всегда — функция была декоративной.
    """
    mapped = map_hits_to_questions([hit("hit_a", fired={"seller_money_impact"})])
    report = build_report({"last_run_at": {c: NOW for c in "ABCDEF"},
                           "hits_by_question": mapped}, NOW)
    q2 = next(a for a in report["answers"] if a["question_no"] == 2)
    assert q2["answer"] == "yes"
    assert q2["hit_ids"] == ["hit_a"]
