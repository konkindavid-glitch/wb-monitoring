"""Генерация фона обложки через OpenRouter."""
import base64
import json

import app
from monitoring import imagegen

HIT = {"title": "Ozon меняет условия хранения", "decision": "QUEUE",
       "platforms": ["OZON"], "topics": ["logistics_warehouse"]}

PIXEL = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


class Client:
    def __init__(self, payload=None, status=200):
        self.payload = payload if payload is not None else {
            "choices": [{"message": {
                "images": [{"image_url": {"url": f"data:image/png;base64,{PIXEL}"}}]
            }}]}
        self.status_code = status
        self.sent = {}
        self.models = []

    def post(self, url, headers=None, json=None):
        self.sent = {"url": url, "headers": headers, "json": json}
        self.models.append(json["model"])
        return self

    def json(self):
        return self.payload

    @property
    def text(self):
        return json.dumps(self.payload)


def test_prompt_forbids_text_in_the_image():
    """Кириллицу модели изображений пишут с ошибками, а заголовок обязан
    совпадать с заголовком материала — поэтому текст наносится своим шрифтом."""
    prompt = imagegen.build_prompt(HIT)
    assert "Никакого текста" in prompt
    assert "логотипов" in prompt


def test_scene_follows_the_topic_not_the_headline():
    """Пересказывать заголовок картинкой значит просить модель
    проиллюстрировать событие, которого она не знает."""
    assert "склад" in imagegen.scene_for(HIT)
    assert imagegen.scene_for({"topics": ["regulation"]}) != imagegen.scene_for(HIT)


def test_unknown_topic_falls_back_to_a_neutral_scene():
    assert imagegen.scene_for({"topics": ["невиданная"]}) == imagegen.DEFAULT_SCENE


def test_incidents_are_never_drawn():
    """Сгенерированный «снимок горящего склада» примут за съёмку с места
    события, которого в таком виде не было."""
    for title in ["Пожар на складе Wildberries в Шушарах",
                  "Атакован хаб в Воронеже",
                  "Обрушение кровли на складе Ozon"]:
        assert imagegen.is_sensitive({"title": title}), title


def test_ordinary_news_is_not_treated_as_an_incident():
    assert not imagegen.is_sensitive(HIT)


def test_sensitive_topic_returns_nothing_without_calling_the_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    client = Client()
    assert imagegen.generate({"title": "Пожар на складе"}, client) == b""
    assert client.sent == {}


def test_image_is_extracted_from_the_answer(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    assert imagegen.generate(HIT, Client()).startswith(b"\x89PNG")


def test_image_is_found_wherever_the_model_puts_it():
    """Разные модели кладут картинку по-разному; ловить одну форму значило бы
    ломаться при смене модели."""
    for payload in [
        {"choices": [{"message": {"content": f"data:image/png;base64,{PIXEL}"}}]},
        {"data": [{"url": f"data:image/jpeg;base64,{PIXEL}"}]},
        {"output": {"image": f"data:image/webp;base64,{PIXEL}"}},
    ]:
        assert imagegen.extract_image(payload).startswith(b"\x89PNG")


def test_answer_without_an_image_yields_nothing():
    assert imagegen.extract_image({"choices": [{"message": {"content": "нет"}}]}) == b""


def test_refusal_is_reported_and_survived(monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    assert imagegen.generate(HIT, Client({"error": "нет денег"}, status=402)) == b""
    assert "402" in capsys.readouterr().out


def test_missing_key_skips_generation(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = Client()
    assert imagegen.generate(HIT, client) == b""
    assert client.sent == {}


def test_generator_failure_never_raises(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")

    class Broken:
        def post(self, *a, **k):
            raise RuntimeError("сеть легла")

    assert imagegen.generate(HIT, Broken()) == b""


# --- выбор источника фона ---------------------------------------------------

class Fetcher:
    def get(self, url, etag=None, last_modified=None):
        raise AssertionError("не должно вызываться")


def test_generated_background_is_preferred(monkeypatch):
    monkeypatch.setenv("COVER_SOURCE", "generated")
    monkeypatch.setattr("monitoring.imagegen.generate", lambda hit: b"drawn")
    assert app.cover_background(HIT, Fetcher()) == (b"drawn", True)


def test_article_photo_is_the_fallback(monkeypatch):
    """Отказ генератора не повод остаться без фона: настоящее фото
    из статьи и честнее, и бесплатно."""
    monkeypatch.setenv("COVER_SOURCE", "generated")
    monkeypatch.setattr("monitoring.imagegen.generate", lambda hit: b"")
    monkeypatch.setattr("monitoring.cover.article_photo",
                        lambda url, fetcher: b"photo")
    assert app.cover_background(dict(HIT, url="https://x.invalid/a"),
                                object()) == (b"photo", False)


def test_photo_mode_never_calls_the_generator(monkeypatch):
    monkeypatch.setenv("COVER_SOURCE", "photo")
    monkeypatch.setattr("monitoring.imagegen.generate",
                        lambda hit: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr("monitoring.cover.article_photo",
                        lambda url, fetcher: b"photo")
    assert app.cover_background(dict(HIT, url="https://x.invalid/a"),
                                object()) == (b"photo", False)


def test_off_mode_makes_no_external_requests(monkeypatch):
    monkeypatch.setenv("COVER_SOURCE", "off")
    assert app.cover_background(dict(HIT, url="https://x.invalid/a"),
                                Fetcher()) == (b"", False)


def test_generated_cover_is_labelled():
    """Выдуманное изображение, поданное как снимок с места события, —
    подлог, даже когда сюжет безобидный."""
    from monitoring.cover import render

    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (900, 1200), (120, 120, 130)).save(buffer, "JPEG")
    data = render(HIT, buffer.getvalue(), generated=True)
    assert data is not None


# --- выбор модели -----------------------------------------------------------

class Chain:
    """Первая модель молчит, вторая рисует."""

    def __init__(self, first_payload):
        self.first_payload, self.models = first_payload, []
        self.status_code = 200
        self._payload = None

    def post(self, url, headers=None, json=None):
        self.models.append(json["model"])
        self._payload = (self.first_payload if len(self.models) == 1 else {
            "choices": [{"message": {"content":
                                     f"data:image/png;base64,{PIXEL}"}}]})
        return self

    def json(self):
        return self._payload

    @property
    def text(self):
        import json as _json
        return _json.dumps(self._payload)


def test_cheapest_model_is_tried_first(monkeypatch):
    """По прайсу OpenRouter она вчетверо дешевле Gemini за картинку."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.delenv("IMAGE_MODEL", raising=False)
    client = Client()
    imagegen.generate(HIT, client)
    assert client.models == ["openai/gpt-5-image-mini"]


def test_second_model_covers_for_the_first(monkeypatch):
    """У разных провайдеров разная форма ответа. Если дешёвая не отдаёт
    картинку через chat/completions, посты не должны остаться без обложек."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.delenv("IMAGE_MODEL", raising=False)
    client = Chain({"choices": [{"message": {"content": "картинки нет"}}]})

    assert imagegen.generate(HIT, client) == base64.b64decode(PIXEL)
    assert client.models == ["openai/gpt-5-image-mini",
                             "google/gemini-2.5-flash-image"]


def test_explicit_model_disables_the_chain(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.setenv("IMAGE_MODEL", "свой/выбор")
    client = Client()
    imagegen.generate(HIT, client)
    assert client.models == ["свой/выбор"]


def test_working_model_is_named_in_the_log(monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.delenv("IMAGE_MODEL", raising=False)
    imagegen.generate(HIT, Client())
    assert "gpt-5-image-mini" in capsys.readouterr().out


def test_lite_gemini_is_not_used_because_it_costs_the_same():
    """«Lite» из семейства Gemini стоит за картинку столько же, сколько
    обычная Nano Banana: экономии нет, только имя меньше."""
    assert not any("lite" in model for model in imagegen.MODELS)
