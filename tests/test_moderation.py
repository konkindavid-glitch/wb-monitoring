import pytest

from monitoring.moderation import (BUTTONS, CALLBACK_LIMIT, EDIT, OUTCOME,
                                   OUTCOME_TOAST, PUBLISH, REJECT,
                                   callback_data, keyboard, outcome_text,
                                   parse_callback)

HIT_ID = "hit_0123456789abcdef"


def test_keyboard_has_the_three_editor_decisions():
    titles = [b["text"] for b in keyboard(HIT_ID)["inline_keyboard"][0]]
    assert titles == ["✅ Запостить", "✏️ Редактировать", "🗑 Удалить"]


def test_every_button_carries_its_hit():
    for button in keyboard(HIT_ID)["inline_keyboard"][0]:
        assert parse_callback(button["callback_data"])[1] == HIT_ID


def test_callback_data_fits_the_telegram_limit():
    """Телеграм режет callback_data на 64 байтах и отдаёт BUTTON_DATA_INVALID —
    кнопка просто не работает, без внятной ошибки."""
    for action, _ in BUTTONS:
        data = callback_data(action, HIT_ID)
        assert len(data.encode("utf-8")) <= CALLBACK_LIMIT


def test_overlong_hit_id_fails_loudly():
    with pytest.raises(ValueError):
        callback_data(PUBLISH, "h" * 80)


def test_roundtrip_of_every_action():
    for action, _ in BUTTONS:
        assert parse_callback(callback_data(action, HIT_ID)) == (action, HIT_ID)


@pytest.mark.parametrize("data", [
    "", "мусор", "other:pub:hit_1", "mod:pub", "mod:unknown:hit_1", "mod:pub:",
])
def test_foreign_and_broken_callbacks_are_ignored(data):
    """В чате может работать не только этот бот. Чужая кнопка не должна
    ни падать, ни толковаться как решение по находке."""
    assert parse_callback(data) is None


def test_outcome_keeps_the_whole_card():
    """По одной строке «отклонено» через неделю не понять, что отклонили."""
    card = "🔴 срочно · 130 баллов\n\nWildberries меняет тариф"
    text = outcome_text(REJECT, card)
    assert text.startswith(OUTCOME[REJECT])
    assert card in text


def test_every_action_has_an_outcome_and_a_toast():
    for action, _ in BUTTONS:
        assert action in OUTCOME and action in OUTCOME_TOAST


def test_edit_is_not_a_terminal_decision():
    """Правка — это продолжение работы над находкой, а не её закрытие:
    состояния находки она менять не должна."""
    from app import STATE_AFTER
    assert EDIT not in STATE_AFTER
    assert STATE_AFTER[PUBLISH] == "HANDED_OFF"
    assert STATE_AFTER[REJECT] == "DROPPED"
