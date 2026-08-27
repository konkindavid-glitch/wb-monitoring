"""Тесты матрицы оценки.

Три эталонных случая взяты из docs/01-triage-scoring.md §2.3 и зафиксированы
здесь намеренно: если правка весов однажды их сдвинет, тест упадёт и напомнит,
что обоснование в документации надо переписывать.
"""
from pathlib import Path

import pytest

from monitoring.config import load_config
from monitoring.scoring import decide, promotion_delta, score_item

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT)
W = CFG.factor_weights()
T = CFG.thresholds()


def s(fired):
    return score_item(fired, W, T)


def test_wb_tariff_change_is_urgent():
    """Эталонный случай 1: изменение тарифа WB из официального источника."""
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
    """Эталонный случай 2: свежая подтверждённая AI-новость без площадки."""
    r = s({
        "ai_link": "AI-инструмент для карточек",
        "authoritative_source": "блог вендора",
        "is_fresh": "вчера",
        "has_practical_takeaway": "применимо к описаниям",
    })
    assert r.score == 55
    assert r.decision == "BACKLOG"


def test_unconfirmed_rumour_still_reaches_queue():
    """Эталонный случай 3 — тот, ради которого матрица не решает публикацию.

    Слух набирает 65 баллов и попадает в рабочую полосу. Это не дефект весов,
    а свойство любой взвешенной суммы: достоверность обменивается на важность.
    Поэтому решение о публикации вынесено за пределы матрицы.
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


def test_max_and_min_match_declared_bounds():
    bounds = CFG.triage["score_bounds"]
    assert sum(w for w in W.values() if w > 0) == bounds["max_positive"]
    assert sum(w for w in W.values() if w < 0) == bounds["min_negative"]


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
