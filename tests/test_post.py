from monitoring.post import TELEGRAM_LIMIT, format_draft, sample_card

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


def test_sample_card_matches_the_reference_case():
    """Эталон обязан совпадать с docs/01 §2.3, иначе он вводит в заблуждение."""
    sample = sample_card()
    total = sum(v["weight"] for v in sample["factors"].values() if v["hit"])
    assert total == sample["score"] == 130
    assert sample["decision"] == "URGENT"


# --- находка, для которой пост не написался ---------------------------------

def test_draft_notice_names_the_finding_and_the_reason():
    """Молча пропустить нельзя: редактор не узнает о материале вовсе.
    Но и выдавать это за пост нельзя."""
    text = format_draft(HIT, "источник не отдал текст статьи")
    assert "не написался" in text
    assert HIT["title"] in text
    assert HIT["url"] in text
    assert "не отдал текст" in text


def test_draft_notice_survives_a_finding_without_a_link():
    assert format_draft({"title": "Заголовок"}, "нет ключа модели")


def test_draft_notice_fits_telegram_limit():
    assert len(format_draft(dict(HIT, title="х" * 5000), "ы" * 5000))         <= TELEGRAM_LIMIT
