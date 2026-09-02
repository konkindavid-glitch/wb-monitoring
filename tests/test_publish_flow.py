"""Путь от нажатия «Запостить» до опубликованного поста.

Пользователь нажал кнопку и не увидел поста — этот файл закрывает
именно тот путь целиком, а не его куски по отдельности.
"""
import app
from monitoring.collectors.base import FetchResult
from monitoring.moderation import PUBLISH, REJECT, callback_data, parse_callback

ARTICLE = ("<html><body><article>"
           "Wildberries с 3 сентября меняет тариф хранения. Базовая ставка "
           "вырастет с 0,3 до 0,5 рубля за литр в сутки. Изменения затронут "
           "все поставки по схеме FBO на складах Коледино и Электросталь. "
           "Продавцам стоит пересчитать себестоимость хранения заранее. "
           "В компании пояснили, что новая тарифная сетка опубликована "
           "в личном кабинете продавца и вступает в силу без переходного "
           "периода. Для товаров, пролежавших на складе дольше шестидесяти "
           "дней, применяется повышенный коэффициент. Селлеры, работающие "
           "по схеме FBS, изменений не почувствуют. Отраслевые эксперты "
           "советуют вывезти неликвид до вступления тарифа в силу."
           "</article></body></html>")

POST = "Wildberries меняет тариф хранения с 3 сентября. Пересчитайте расходы."


class FakeRepo:
    def __init__(self):
        self.decisions, self.states = [], []
        self.hits = {"hit_real": {"hit_id": "hit_real",
                                  "title": "Wildberries меняет тариф хранения",
                                  "url": "https://x.invalid/wb",
                                  "score": 130, "decision": "URGENT",
                                  "factors": {}, "platforms": [], "topics": []}}

    def save_decision(self, hit_id, action, chat_id, message_id=None,
                      prompt_message_id=None, editor_id=None):
        self.decisions.append((hit_id, action))
        return "dec_1"

    def set_hit_state(self, hit_id, state):
        self.states.append((hit_id, state))

    def hit_by_id(self, hit_id):
        return self.hits.get(hit_id)

    def rollback(self):
        pass


class FakeFetcher:
    def __init__(self, html=ARTICLE, status=200):
        self.html, self.status = html, status

    def get(self, url, etag=None, last_modified=None):
        return FetchResult(self.status, self.html)


class FakeJudge:
    def __init__(self, reply=POST):
        self.reply = reply

    def complete(self, prompt):
        return self.reply


class Telegram:
    """Записывает всё, что ушло в Телеграм, вместо отправки."""

    def __init__(self):
        self.sent, self.edits, self.deleted, self.answered = [], [], [], []

    def install(self, monkeypatch, env_channel=None):
        monkeypatch.setattr(app, "send_card", self.send_card)
        monkeypatch.setattr(app, "replace_text", self.replace_text)
        monkeypatch.setattr(app, "delete_message", self.delete_message)
        monkeypatch.setattr(app, "answer_callback",
                            lambda *a, **k: self.answered.append(a) or True)
        monkeypatch.setattr(app, "render_cover", lambda hit, fetcher=None: b"jpeg")
        if env_channel is None:
            monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)
        else:
            monkeypatch.setenv("TELEGRAM_CHANNEL_ID", env_channel)

    def send_card(self, text, token, chat_id, *, cover=None, reply_markup=None):
        self.sent.append({"text": text, "chat_id": chat_id, "cover": cover})
        return 500 + len(self.sent)

    def replace_text(self, message_id, text, token, chat_id, *,
                     has_caption=False, reply_markup=None):
        self.edits.append({"text": text, "reply_markup": reply_markup})
        return True

    def delete_message(self, message_id, token, chat_id):
        self.deleted.append(message_id)
        return True


def make_deps(repo=None, fetcher=None, judge=None):
    return app.Deps(cfg=None, repo=repo or FakeRepo(),
                    fetcher=fetcher or FakeFetcher(),
                    judge=judge or FakeJudge(),
                    token="T", chat_id="100")


def press(action, hit_id="hit_real"):
    return {"id": "cb1", "data": callback_data(action, hit_id),
            "from": {"id": 777},
            "message": {"message_id": 42, "chat": {"id": 100},
                        "caption": "🔴 срочно · 130 баллов"}}


def test_publish_writes_a_post_and_sends_it_to_the_channel(monkeypatch):
    tg = Telegram()
    tg.install(monkeypatch, env_channel="-1001234")
    deps = make_deps()

    app.handle_callback(press(PUBLISH), deps)

    assert len(tg.sent) == 1
    assert tg.sent[0]["chat_id"] == "-1001234"
    assert POST in tg.sent[0]["text"]


def test_published_post_carries_the_source_link(monkeypatch):
    """Ссылку дописывает код, а не модель: единственное место,
    где её нельзя переврать."""
    tg = Telegram()
    tg.install(monkeypatch, env_channel="-1001234")
    app.handle_callback(press(PUBLISH), make_deps())
    assert "https://x.invalid/wb" in tg.sent[0]["text"]


def test_post_goes_to_the_moderation_chat_when_no_channel_is_set(monkeypatch):
    """Без канала пост обязан всё равно появиться: молчаливое «никуда»
    и есть та жалоба, с которой всё началось."""
    tg = Telegram()
    tg.install(monkeypatch, env_channel=None)
    app.handle_callback(press(PUBLISH), make_deps())

    assert len(tg.sent) == 1
    assert tg.sent[0]["chat_id"] == "100"
    assert "канал не задан" in tg.edits[-1]["text"]


def test_publishing_records_the_decision_and_hands_the_hit_off(monkeypatch):
    tg = Telegram()
    tg.install(monkeypatch, env_channel="-1001234")
    repo = FakeRepo()
    app.handle_callback(press(PUBLISH), make_deps(repo=repo))

    assert repo.decisions == [("hit_real", PUBLISH)]
    assert repo.states == [("hit_real", "HANDED_OFF")]


def test_buttons_disappear_while_the_post_is_being_written(monkeypatch):
    """Второе нажатие не должно опубликовать пост дважды."""
    tg = Telegram()
    tg.install(monkeypatch, env_channel="-1001234")
    app.handle_callback(press(PUBLISH), make_deps())

    assert "Пишу пост" in tg.edits[0]["text"]
    assert tg.edits[0]["reply_markup"] is None


def test_failure_says_why_and_gives_the_buttons_back(monkeypatch):
    """Источник может ответить позже, ключ — пополниться. Попытку надо
    дать повторить, а не хоронить находку."""
    tg = Telegram()
    tg.install(monkeypatch, env_channel="-1001234")
    deps = make_deps(fetcher=FakeFetcher(html="", status=403))

    app.handle_callback(press(PUBLISH), deps)

    assert tg.sent == []
    last = tg.edits[-1]
    assert "не написан" in last["text"]
    assert "не отдал текст" in last["text"]
    assert last["reply_markup"] is not None


def test_failed_publication_does_not_hand_the_hit_off(monkeypatch):
    tg = Telegram()
    tg.install(monkeypatch, env_channel="-1001234")
    repo = FakeRepo()
    app.handle_callback(press(PUBLISH),
                        make_deps(repo=repo, fetcher=FakeFetcher("", 403)))
    assert repo.states == []


def test_invented_numbers_block_publication(monkeypatch):
    """Выдуманный процент в посте для селлеров опаснее отсутствия поста."""
    tg = Telegram()
    tg.install(monkeypatch, env_channel="-1001234")
    deps = make_deps(judge=FakeJudge("Комиссия вырастет на 87 процентов."))

    app.handle_callback(press(PUBLISH), deps)

    assert tg.sent == []
    assert "87" in tg.edits[-1]["text"]


def test_reject_deletes_the_card_and_drops_the_hit(monkeypatch):
    tg = Telegram()
    tg.install(monkeypatch, env_channel="-1001234")
    repo = FakeRepo()
    app.handle_callback(press(REJECT), make_deps(repo=repo))

    assert tg.deleted == [42]
    assert repo.states == [("hit_real", "DROPPED")]
    assert tg.sent == []


def test_sample_card_can_be_published_although_it_is_not_in_the_database(
        monkeypatch):
    """Проверочная карточка приходит при каждом старте, и её кнопки
    обязаны работать — иначе первое же нажатие выглядит как поломка."""
    tg = Telegram()
    tg.install(monkeypatch, env_channel="-1001234")
    deps = make_deps()

    app.handle_callback(press(PUBLISH, "hit_sample"), deps)

    assert len(tg.sent) == 1


def test_unknown_hit_reports_instead_of_failing_silently(monkeypatch):
    tg = Telegram()
    tg.install(monkeypatch, env_channel="-1001234")
    app.handle_callback(press(PUBLISH, "hit_missing"), make_deps())

    assert tg.sent == []
    assert "не найдена" in tg.edits[-1]["text"]


def test_press_survives_a_broken_database(monkeypatch):
    """База может лежать. Редактор всё равно должен увидеть результат,
    а не тишину."""
    class Broken(FakeRepo):
        def save_decision(self, *a, **k):
            raise RuntimeError("connection is closed")

    tg = Telegram()
    tg.install(monkeypatch, env_channel="-1001234")
    app.handle_callback(press(PUBLISH), make_deps(repo=Broken()))

    assert len(tg.sent) == 1


def test_callback_roundtrip_matches_what_the_keyboard_emits():
    assert parse_callback(callback_data(PUBLISH, "hit_real")) == (PUBLISH,
                                                                  "hit_real")


# --- поиск канала ----------------------------------------------------------

def channel_post(chat_id=-1001234567890, title="Новости селлеров",
                 username=None, chat_type="channel"):
    chat = {"id": chat_id, "type": chat_type, "title": title}
    if username:
        chat["username"] = username
    return {"message_id": 7, "chat": chat, "text": "проверка"}


class Notes:
    def __init__(self):
        self.sent = []

    def install(self, monkeypatch):
        monkeypatch.setattr(app, "send",
                            lambda text, token, chat: self.sent.append(text) or True)
        app._ANNOUNCED_CHATS.clear()


def test_bot_reports_the_channel_id_itself(monkeypatch):
    """Узнавать id приватного канала вручную неудобно, а гонять человека
    к сторонним ботам за идентификатором его же канала — плохой совет."""
    notes = Notes()
    notes.install(monkeypatch)
    monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)

    app.handle_channel_post(channel_post(), make_deps())

    assert "TELEGRAM_CHANNEL_ID=-1001234567890" in notes.sent[0]
    assert "Новости селлеров" in notes.sent[0]


def test_public_channel_is_offered_by_username(monkeypatch):
    """Bot API принимает @имя напрямую — числовой id тогда лишний."""
    notes = Notes()
    notes.install(monkeypatch)
    monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)

    app.handle_channel_post(channel_post(username="sellers"), make_deps())

    assert "TELEGRAM_CHANNEL_ID=@sellers" in notes.sent[0]


def test_configured_channel_is_not_announced(monkeypatch):
    notes = Notes()
    notes.install(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-1001234567890")

    app.handle_channel_post(channel_post(), make_deps())
    assert notes.sent == []


def test_channel_configured_by_username_is_not_announced(monkeypatch):
    notes = Notes()
    notes.install(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "@sellers")

    app.handle_channel_post(channel_post(username="sellers"), make_deps())
    assert notes.sent == []


def test_the_same_channel_is_announced_only_once(monkeypatch):
    """Одно сообщение полезно, десять — шум, из-за которого перестают
    читать бота."""
    notes = Notes()
    notes.install(monkeypatch)
    monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)

    for _ in range(3):
        app.handle_channel_post(channel_post(), make_deps())
    assert len(notes.sent) == 1


def test_group_chats_are_not_mistaken_for_channels(monkeypatch):
    notes = Notes()
    notes.install(monkeypatch)
    monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)

    app.handle_channel_post(channel_post(chat_type="supergroup"), make_deps())
    assert notes.sent == []


def test_refusal_reason_reaches_the_editor(monkeypatch):
    """«bot is not a member of the channel chat» говорит, что делать,
    а «Телеграм не принял» — нет."""
    tg = Telegram()
    tg.install(monkeypatch, env_channel="-1001234")
    monkeypatch.setattr(app, "send_card", lambda *a, **k: None)
    monkeypatch.setattr("monitoring.delivery.last_error",
                        lambda: "Forbidden: bot is not a member of the channel chat")

    app.handle_callback(press(PUBLISH), make_deps())

    assert "not a member of the channel" in tg.edits[-1]["text"]
    assert tg.edits[-1]["reply_markup"] is not None
