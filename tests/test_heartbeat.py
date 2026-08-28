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
