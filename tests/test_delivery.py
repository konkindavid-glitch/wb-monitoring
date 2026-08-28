from monitoring.delivery import TELEGRAM_LIMIT, format_digest, format_urgent

HIT = {
    "hit_id": "hit_a1", "title": "WB меняет тариф хранения", "score": 130,
    "decision": "URGENT", "url": "https://x.invalid/a",
    "factors": {
        "platform_wb": {"hit": True, "weight": 25, "why": "тарифы WB"},
        "seller_money_impact": {"hit": True, "weight": 25, "why": "хранение"},
        "ai_link": {"hit": False, "weight": 0},
    },
}
REPORT = {"urgent_count": 1, "queue_count": 2}


def test_urgent_shows_score_title_and_link():
    text = format_urgent(HIT)
    assert "130" in text
    assert "WB меняет тариф хранения" in text
    assert "https://x.invalid/a" in text


def test_urgent_lists_only_fired_factors_with_rationale():
    text = format_urgent(HIT)
    assert "тарифы WB" in text
    assert "хранение" in text
    assert "ai_link" not in text


def test_urgent_says_it_is_not_permission_to_publish():
    """URGENT означает «проверить первым», и в сообщении это должно быть видно."""
    assert "проверить" in format_urgent(HIT).lower()
    assert "не публиковать" in format_urgent(HIT).lower()


def test_digest_fits_telegram_limit():
    many = [dict(HIT, hit_id=f"hit_{i}", title=f"Материал {i} " + "х" * 200)
            for i in range(60)]
    text = format_digest(many, [], REPORT)
    assert len(text) <= TELEGRAM_LIMIT


def test_digest_says_how_many_were_cut():
    many = [dict(HIT, hit_id=f"hit_{i}", title=f"Материал {i} " + "х" * 200)
            for i in range(60)]
    assert "и ещё" in format_digest(many, [], REPORT)


def test_digest_reports_degraded_sources():
    assert "src_wb_tariffs" in format_digest([], ["src_wb_tariffs"], REPORT)


def test_empty_digest_says_so_plainly():
    text = format_digest([], [], {"urgent_count": 0, "queue_count": 0})
    assert "нет" in text.lower()
