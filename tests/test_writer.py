"""Генератор постов. Главное здесь — что выдуманные числа не проходят.

Пост для селлеров с придуманным процентом или сроком опаснее отсутствия
поста: по нему пересчитают себестоимость или пропустят срок.
"""
from monitoring.writer import (MIN_SOURCE_CHARS, numbers, unsupported_numbers,
                               write_post)

SOURCE = ("Wildberries с 3 сентября меняет тариф хранения. Базовая ставка "
          "вырастет с 0,3 до 0,5 рубля за литр в сутки, для складов "
          "Коледино и Электросталь коэффициент 1,5. Изменения затронут "
          "все поставки по схеме FBO. " * 3)
HIT = {"title": "Wildberries меняет тариф хранения", "url": "https://x.invalid/wb"}


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def test_numbers_ignore_thousands_separators():
    """«1 500» и «1500» — одно число: придираться к пробелу значит
    браковать верные посты."""
    assert numbers("ставка 1 500 рублей") == numbers("ставка 1500 рублей")


def test_number_present_in_source_is_supported():
    assert unsupported_numbers("ставка с 3 сентября", SOURCE) == []


def test_invented_number_is_caught():
    assert unsupported_numbers("комиссия вырастет на 27%", SOURCE) == ["27"]


def test_post_with_invented_numbers_is_rejected_after_retry():
    client = FakeClient("Комиссия вырастет на 27%.", "И ещё на 42 процента.")
    result = write_post(HIT, SOURCE, client)
    assert not result
    assert "42" in result.reason


def test_retry_names_the_invented_numbers():
    """Модель чаще всего просто убирает цифру, если сказать какую."""
    client = FakeClient("Вырастет на 27%.", "Тариф хранения вырастет с 3 сентября.")
    assert write_post(HIT, SOURCE, client)
    assert "27" in client.prompts[1]


def test_clean_post_passes_on_first_try():
    client = FakeClient("Wildberries меняет тариф хранения с 3 сентября.")
    result = write_post(HIT, SOURCE, client)
    assert result.text.startswith("Wildberries")
    assert len(client.prompts) == 1


def test_thin_source_is_refused_without_calling_the_model():
    """По одному заголовку пост не пишется: это было бы сочинительство."""
    client = FakeClient("что угодно")
    result = write_post(HIT, "коротко", client)
    assert not result
    assert "не отдал текст" in result.reason
    assert client.prompts == []


def test_missing_model_is_reported_not_swallowed():
    result = write_post(HIT, SOURCE, None)
    assert not result
    assert "ключа модели" in result.reason


def test_model_failure_is_reported():
    class Broken:
        def complete(self, prompt):
            raise RuntimeError("402 Payment Required")

    result = write_post(HIT, SOURCE, Broken())
    assert not result
    assert "402" in result.reason


def test_source_threshold_is_a_real_guard():
    assert MIN_SOURCE_CHARS >= 200


# --- пост обязан влезать в подпись к фото -----------------------------------

def test_overlong_post_is_rewritten_not_chopped():
    """Просить у модели 900 знаков и не проверять результат — значит
    не иметь предела вовсе: модель писала 1200, и пост разъезжался
    на два сообщения."""
    from monitoring.writer import POST_LIMIT

    long_one = "Тариф меняется с 3 сентября. " * 50
    client = FakeClient(long_one, "Короткий пост про 3 сентября.")

    result = write_post(HIT, SOURCE, client)

    assert result.text == "Короткий пост про 3 сентября."
    assert "занял" in client.prompts[1]
    assert str(POST_LIMIT) in client.prompts[1]


def test_post_that_stays_long_is_cut_on_a_sentence_boundary():
    """Тупая обрезка рвёт слово пополам и оставляет пост без концовки."""
    from monitoring.delivery import CAPTION_LIMIT
    from monitoring.writer import POST_LIMIT

    long_one = "Тариф меняется с 3 сентября. " * 50
    result = write_post(HIT, SOURCE, FakeClient(long_one, long_one))

    assert len(result.text) <= POST_LIMIT == CAPTION_LIMIT
    assert result.text.endswith(".")


def test_short_post_is_untouched():
    text = "Wildberries меняет тариф хранения с 3 сентября."
    assert write_post(HIT, SOURCE, FakeClient(text)).text == text


def test_fit_keeps_whole_sentences():
    from monitoring.writer import fit

    text = "Первое предложение. " * 100
    cut = fit(text, 200)
    assert len(cut) <= 200
    assert cut.endswith("предложение.")


def test_fit_leaves_short_text_alone():
    from monitoring.writer import fit
    assert fit("коротко", 200) == "коротко"
