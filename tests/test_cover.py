"""Обложка. Главное здесь — проверка кириллицы.

Дважды подряд шрифт объявлялся пригодным, а карточка уходила с квадратами
вместо букв: `getmask(c).getbbox()` у квадрата `.notdef` не пустой, и наивная
проверка проходила ложно. Тесты закрывают именно этот способ ошибиться.
"""
from monitoring import cover

pytest_plugins = ()


def test_bundled_font_really_has_cyrillic():
    fonts = cover.find_fonts()
    assert fonts is not None, "в репозитории должен лежать шрифт с кириллицей"
    assert cover._renders_cyrillic(fonts[0])


def test_font_without_cyrillic_is_rejected(tmp_path):
    """Страховка от повторения: шрифт без кириллицы обязан быть отвергнут.

    Строится усечением настоящего шрифта до латиницы — так проверяется
    сам метод, а не наличие подходящего файла.
    """
    fontTools = __import__("importlib").util.find_spec("fontTools")
    if fontTools is None:
        import pytest
        pytest.skip("fontTools не установлен")
    from fontTools import subset
    from fontTools.ttLib import TTFont

    stripped = tmp_path / "latin-only.ttf"
    font = TTFont(str(cover.find_fonts()[0]))
    subset.Subsetter(subset.Options()).subset(font)  # только .notdef и латиница
    font.save(str(stripped))
    assert cover._renders_cyrillic(stripped) is False


def test_missing_font_yields_no_cover_instead_of_crashing(monkeypatch):
    """Отсутствие шрифта не повод не доставить находку: карточка уходит
    текстом, а не теряется вместе с обложкой."""
    monkeypatch.setattr(cover, "FONT_CANDIDATES", [])
    monkeypatch.setattr(cover, "_CACHED", None)
    assert cover.render({"title": "Заголовок", "score": 90}) is None


def open_cover(data):
    import io

    from PIL import Image
    return Image.open(io.BytesIO(data))


def test_render_returns_an_image_of_post_proportions():
    """4:5 — вертикаль, которую Телеграм показывает в ленте крупнее всего."""
    image = open_cover(cover.render({"title": "Wildberries меняет тариф",
                                     "decision": "URGENT",
                                     "platforms": ["WILDBERRIES"]}))
    assert image.format == "JPEG"
    assert image.size == (cover.WIDTH, cover.HEIGHT) == (1080, 1350)


def test_platform_changes_the_colour():
    """Принадлежность площадке должна читаться до текста."""
    def corner(platform):
        data = cover.render({"title": "Т", "decision": "URGENT",
                             "platforms": [platform]})
        return open_cover(data).convert("RGB").getpixel((5, 5))

    assert corner("WILDBERRIES") != corner("OZON")


def test_photo_shows_through_at_the_top():
    """Затемнение по всей высоте съедало фотографию целиком — оставалась
    почти чёрная картинка, ради которой незачем было её скачивать."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1600, 900), (235, 235, 245)).save(buffer, "JPEG")

    rendered = open_cover(cover.render(
        {"title": "Заголовок", "decision": "QUEUE", "platforms": ["OZON"]},
        buffer.getvalue())).convert("RGB")

    assert min(rendered.getpixel((540, 60))) > 170       # верх — светлый
    assert max(rendered.getpixel((540, 1310))) < 90      # низ — тёмный


def test_broken_photo_falls_back_to_the_plain_cover():
    """Битая картинка не повод остаться без обложки."""
    assert cover.render({"title": "Заголовок", "decision": "QUEUE"},
                        b"\x00\x01not an image") is not None


def test_og_image_is_found_in_both_attribute_orders():
    assert cover.og_image_url(
        '<meta property="og:image" content="https://x.invalid/a.jpg">'
    ) == "https://x.invalid/a.jpg"
    assert cover.og_image_url(
        '<meta content="https://x.invalid/b.jpg" property="og:image">'
    ) == "https://x.invalid/b.jpg"


def test_relative_og_image_is_resolved_against_the_page():
    assert cover.og_image_url(
        '<meta property="og:image" content="/img/a.jpg">',
        "https://site.invalid/news/1") == "https://site.invalid/img/a.jpg"


def test_protocol_relative_og_image_gets_https():
    assert cover.og_image_url(
        '<meta property="og:image" content="//cdn.invalid/a.jpg">'
    ) == "https://cdn.invalid/a.jpg"


def test_page_without_og_image_yields_nothing():
    assert cover.og_image_url("<html><body>без картинки</body></html>") == ""


def test_article_photo_never_raises():
    """Обложка не стоит того, чтобы из-за неё не ушёл пост."""
    class Broken:
        def get(self, url, etag=None, last_modified=None):
            raise RuntimeError("источник лёг")

    assert cover.article_photo("https://x.invalid/a", Broken()) == b""


def test_unknown_band_does_not_crash():
    assert cover.render({"title": "Т", "score": 1, "decision": None}) is not None


def test_very_long_title_is_bounded():
    """Заголовок в тысячу символов не должен уезжать за нижний край."""
    assert cover.render({"title": "слово " * 300, "score": 40,
                         "decision": "QUEUE"}) is not None
