from monitoring.post import TELEGRAM_LIMIT, format_card, sample_card

HIT = {
    "hit_id": "hit_a1",
    "title": "Wildberries меняет тариф хранения с 3 сентября",
    "url": "https://x.invalid/wb",
    "score": 130,
    "decision": "URGENT",
    "platforms": ["WILDBERRIES"],
    "topics": ["seller_money", "logistics_warehouse"],
    "factors": {
        "platform_wb": {"hit": True, "weight": 25, "why": "раздел тарифов WB"},
        "seller_money_impact": {"hit": True, "weight": 25, "why": "расходы на хранение"},
        "no_confirmation": {"hit": False, "weight": 0},
        "ai_link": {"hit": False, "weight": 0},
    },
}


def test_card_shows_score_band_and_title():
    text = format_card(HIT)
    assert "130" in text
    assert "срочно" in text
    assert "Wildberries меняет тариф хранения" in text


def test_card_breaks_the_score_down():
    """Редактор должен видеть, из чего сложились баллы, а не только сумму.

    Иначе оценке нельзя ни доверять, ни возразить, а откалибровать пороги
    по ней тем более невозможно.
    """
    text = format_card(HIT)
    assert "+25 · раздел тарифов WB" in text
    assert "+25 · расходы на хранение" in text


def test_card_hides_factors_that_did_not_fire():
    text = format_card(HIT)
    assert "no_confirmation" not in text
    assert "ai_link" not in text


def test_factors_are_ordered_by_weight():
    hit = dict(HIT, factors={
        "ai_link": {"hit": True, "weight": 10, "why": "мелкий"},
        "platform_wb": {"hit": True, "weight": 25, "why": "крупный"},
    })
    text = format_card(hit)
    assert text.index("крупный") < text.index("мелкий")


def test_card_translates_topics_and_platforms_to_russian():
    text = format_card(HIT)
    assert "деньги селлеров" in text
    assert "логистика и склады" in text
    assert "seller_money" not in text


def test_card_states_that_score_is_not_permission_to_publish():
    assert "не разрешение" in format_card(HIT)


def test_test_flag_marks_the_card_as_a_check():
    plain = format_card(HIT)
    marked = format_card(HIT, is_test=True)
    assert "ПРОВЕРКА СВЯЗИ" in marked
    assert "ПРОВЕРКА СВЯЗИ" not in plain


def test_card_fits_telegram_limit():
    huge = dict(HIT, title="х" * 5000,
                factors={f"f{i}": {"hit": True, "weight": 10, "why": "ы" * 200}
                         for i in range(40)})
    assert len(format_card(huge)) <= TELEGRAM_LIMIT


def test_card_survives_missing_fields():
    """Находка из базы может прийти без части полей — падать нельзя."""
    text = format_card({"title": "Заголовок", "score": 50, "decision": "BACKLOG"})
    assert "Заголовок" in text
    assert "в запас" in text


def test_sample_card_matches_the_reference_case():
    """Эталон обязан совпадать с docs/01 §2.3, иначе он вводит в заблуждение."""
    sample = sample_card()
    total = sum(v["weight"] for v in sample["factors"].values() if v["hit"])
    assert total == sample["score"] == 130
    assert sample["decision"] == "URGENT"
