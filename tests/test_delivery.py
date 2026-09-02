from monitoring import delivery
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


# --- ошибки не должны молчать ----------------------------------------------

class Reply:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        if self.payload is None:
            raise ValueError("не JSON")
        return self.payload


def test_telegram_refusal_is_logged_not_swallowed(monkeypatch, capsys):
    """Молчаливый return None делал отказ Телеграма неотличимым от
    «всё хорошо, просто ничего не пришло» — нажатая кнопка не давала
    ни результата, ни следа."""
    monkeypatch.setattr(delivery.httpx, "post", lambda *a, **k: Reply(
        {"ok": False, "error_code": 400,
         "description": "message to edit not found"}))

    assert delivery._call("https://x/botT/editMessageText", {}) is None
    out = capsys.readouterr().out
    assert "editMessageText" in out
    assert "message to edit not found" in out


def test_network_failure_is_logged(monkeypatch, capsys):
    def boom(*a, **k):
        raise delivery.httpx.ConnectError("нет сети")

    monkeypatch.setattr(delivery.httpx, "post", boom)
    assert delivery._call("https://x/botT/sendMessage", {}) is None
    assert "sendMessage" in capsys.readouterr().out


def test_webhook_conflict_on_get_updates_is_visible(monkeypatch, capsys):
    """409 значит установленный вебхук: кнопки мертвы полностью, и это
    обязано быть видно, а не выясняться перебором догадок."""
    monkeypatch.setattr(delivery.httpx, "post", lambda *a, **k: Reply(
        {"ok": False, "error_code": 409,
         "description": "Conflict: can't use getUpdates method while webhook "
                        "is active"}))

    assert delivery.get_updates("T", 0, timeout=0) == []
    assert "409" in capsys.readouterr().out


def test_empty_update_list_is_quiet(monkeypatch, capsys):
    """Пустой список — норма, а не повод шуметь в лог каждую минуту."""
    monkeypatch.setattr(delivery.httpx, "post",
                        lambda *a, **k: Reply({"ok": True, "result": []}))

    assert delivery.get_updates("T", 0, timeout=0) == []
    assert capsys.readouterr().out == ""


def test_webhook_info_reports_the_configured_url(monkeypatch):
    monkeypatch.setattr(delivery.httpx, "post", lambda *a, **k: Reply(
        {"ok": True, "result": {"url": "https://example.invalid/hook"}}))
    assert delivery.webhook_info("T")["url"] == "https://example.invalid/hook"


def test_webhook_info_without_token_does_not_call_telegram(monkeypatch):
    monkeypatch.setattr(delivery.httpx, "post", lambda *a, **k: 1 / 0)
    assert delivery.webhook_info("") == {}
