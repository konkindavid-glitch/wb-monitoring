from monitoring.delivery import TELEGRAM_LIMIT, format_digest

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
