"""Обложка к посту: фото источника с крупным заголовком поверх.

Собирается локально через Pillow — бесплатно, предсказуемо, без обращений
к генераторам картинок. Фон берётся из самой статьи (og:image): это честно
(снимок относится к событию), бесплатно и всегда по теме. Нарисованная
моделью «фотография склада» выглядела бы так же, но обозначала бы событие,
которого не было.

Нет фото — остаётся фирменная плашка с градиентом по цвету площадки.
Пост уходит в любом случае: обложка украшает, а не решает.

Шрифт лежит в репозитории намеренно: в контейнере Амверы Linux, шрифтов
Windows там нет. Взят DejaVu Sans — полная кириллица и лицензия, прямо
разрешающая распространение вместе с программой, в отличие от Arial
и Segoe UI, которые в публичный репозиторий класть нельзя.
"""
import io
import re
from pathlib import Path

# 4:5 — вертикаль, которую Телеграм показывает крупнее всего в ленте.
WIDTH, HEIGHT = 1080, 1350
MARGIN = 72

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# Порядок поиска: сначала положенное в репозиторий, потом системные пути.
FONT_CANDIDATES = [
    (FONTS_DIR / "DejaVuSans-Bold.ttf", FONTS_DIR / "DejaVuSans.ttf"),
    (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
     Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")),
    (Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
     Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf")),
]

_CACHED = None

# Цвет плашки — по площадке, чтобы принадлежность читалась до текста.
PLATFORM_STYLE = {
    "WILDBERRIES": {"accent": (203, 17, 171), "label": "WILDBERRIES"},
    "OZON": {"accent": (0, 91, 255), "label": "OZON"},
    "YANDEX_MARKET": {"accent": (255, 204, 0), "label": "ЯНДЕКС МАРКЕТ"},
    "CROSS_PLATFORM": {"accent": (99, 102, 241), "label": "МАРКЕТПЛЕЙСЫ"},
}
DEFAULT_PLATFORM = PLATFORM_STYLE["CROSS_PLATFORM"]

# Надпись на плашке — по полосе очереди, а не по содержанию: додумывать
# «срочно» там, где его нет, значит обещать читателю то, чего в посте нет.
BAND_LABEL = {
    "URGENT": "СРОЧНО",
    "QUEUE": "ВАЖНО",
    "BACKLOG": "К СВЕДЕНИЮ",
    "DROP": "К СВЕДЕНИЮ",
}

BRAND = "Карта мониторинга"

_OG_IMAGE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image|twitter:image)["']"""
    r"""[^>]*content\s*=\s*["']([^"']+)["']""", re.I)
_OG_IMAGE_REVERSED = re.compile(
    r"""<meta[^>]+content\s*=\s*["']([^"']+)["'][^>]*"""
    r"""(?:property|name)\s*=\s*["'](?:og:image|twitter:image)["']""", re.I)


def _renders_cyrillic(path) -> bool:
    """Есть ли в шрифте настоящая кириллица.

    Проверять наличие глифа по getmask(...).getbbox() нельзя: у квадрата
    .notdef bbox тоже есть, и проверка проходит ложно — на этом я уже
    обжёгся, обложка ушла с квадратами вместо букв. Надёжный способ —
    сравнить растр буквы с растром символа из приватной зоны, которого
    в шрифте заведомо нет.
    """
    from PIL import Image, ImageDraw, ImageFont

    def raster(char):
        font = ImageFont.truetype(str(path), 48)
        image = Image.new("L", (96, 96), 0)
        ImageDraw.Draw(image).text((5, 5), char, font=font, fill=255)
        return image.tobytes()

    try:
        notdef = raster("")
        return all(raster(c) != notdef for c in "ЖЯЮДЩ")
    except Exception:
        return False


def find_fonts():
    """Пара (жирный, обычный) с кириллицей или None."""
    global _CACHED
    if _CACHED is not None:
        return _CACHED or None

    for bold, regular in FONT_CANDIDATES:
        if bold.exists() and regular.exists() and _renders_cyrillic(bold):
            _CACHED = (bold, regular)
            return _CACHED
    _CACHED = ()
    return None


def fonts_available() -> bool:
    return find_fonts() is not None


# --- фон из статьи ----------------------------------------------------------

def og_image_url(html: str, page_url: str = "") -> str:
    """Адрес картинки-превью статьи. Пустая строка, если её нет."""
    for pattern in (_OG_IMAGE, _OG_IMAGE_REVERSED):
        found = pattern.search(html or "")
        if not found:
            continue
        url = found.group(1).strip()
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/") and page_url:
            root = "/".join(page_url.split("/", 3)[:3])
            return root + url
        if url.startswith("http"):
            return url
    return ""


def article_photo(url: str, fetcher) -> bytes:
    """Картинка-превью статьи или пусто. Никогда не бросает.

    Обложка не стоит того, чтобы из-за неё не ушёл пост, поэтому любая
    осечка здесь — это просто отсутствие фона, а не ошибка.
    """
    try:
        page = fetcher.get(url)
        if page.status != 200 or not page.text:
            return b""
        image_url = og_image_url(page.text, url)
        return fetcher.get_bytes(image_url) if image_url else b""
    except Exception:
        return b""


# --- рисование --------------------------------------------------------------

def _fill(image, width: int, height: int):
    """Кадрирует по центру под нужное соотношение, без полей и растяжения."""
    from PIL import Image

    scale = max(width / image.width, height / image.height)
    resized = image.resize((max(1, round(image.width * scale)),
                            max(1, round(image.height * scale))),
                           Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _scrim(image):
    """Затемнение снизу под заголовок.

    Плотное по всей высоте съедало фотографию целиком — оставалась почти
    чёрная картинка, ради которой незачем было её и скачивать. Верх остаётся
    почти чистым, темнеет только низ, где лежит текст.
    """
    from PIL import Image

    overlay = Image.new("L", (1, HEIGHT))
    for y in range(HEIGHT):
        position = y / HEIGHT
        if position < 0.42:
            alpha = int(70 * (position / 0.42) ** 2)
        else:
            # Ниже 42% высоты — разгон до почти непрозрачного к самому низу.
            alpha = 70 + int(175 * ((position - 0.42) / 0.58) ** 1.4)
        overlay.putpixel((0, y), min(245, alpha))
    mask = overlay.resize((WIDTH, HEIGHT))
    dark = Image.new("RGB", (WIDTH, HEIGHT), (8, 8, 12))
    return Image.composite(dark, image, mask)


def _gradient(draw, top, bottom):
    for y in range(HEIGHT):
        t = y / HEIGHT
        draw.line([(0, y), (WIDTH, y)],
                  fill=tuple(int(top[i] + (bottom[i] - top[i]) * t)
                             for i in range(3)))


def _wrap(draw, text, font, max_width):
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _badge(draw, xy, text, font, accent):
    """Плашка с текстом. Возвращает её высоту."""
    x, y = xy
    pad_x, pad_y = 26, 14
    width = draw.textlength(text, font=font)
    height = font.size + pad_y * 2
    draw.rounded_rectangle([x, y, x + width + pad_x * 2, y + height],
                           radius=height // 2, fill=accent)
    draw.text((x + pad_x, y + pad_y - 2), text, font=font, fill=(255, 255, 255))
    return height


def render(hit: dict, photo: bytes = None):
    """Обложка в PNG или None, если подходящего шрифта нет.

    None вместо исключения намеренно: отсутствие шрифта не повод не доставить
    находку — она уйдёт текстом.
    """
    from PIL import Image, ImageDraw, ImageFont

    found = find_fonts()
    if not found:
        return None
    font_bold, font_regular = found

    platforms = hit.get("platforms") or []
    style = PLATFORM_STYLE.get(platforms[0] if platforms else "",
                               DEFAULT_PLATFORM)
    accent = style["accent"]

    canvas = None
    if photo:
        try:
            canvas = _scrim(_fill(Image.open(io.BytesIO(photo)).convert("RGB"),
                                  WIDTH, HEIGHT))
        except Exception:
            canvas = None

    if canvas is None:
        canvas = Image.new("RGB", (WIDTH, HEIGHT), (16, 16, 20))
        _gradient(ImageDraw.Draw(canvas),
                  tuple(max(0, c - 60) for c in accent), (16, 16, 20))

    draw = ImageDraw.Draw(canvas)
    label_font = ImageFont.truetype(str(font_bold), 34)
    small_font = ImageFont.truetype(str(font_regular), 30)

    # Заголовок: кегль уменьшается, пока не влезет в шесть строк.
    title = (hit.get("title") or "без заголовка").upper()
    for size in (84, 74, 64, 56, 48, 42):
        title_font = ImageFont.truetype(str(font_bold), size)
        lines = _wrap(draw, title, title_font, WIDTH - MARGIN * 2)[:6]
        if len(lines) <= 6:
            break

    # Блок текста прижат к низу: сверху остаётся фотография, а не заливка.
    line_height = int(title_font.size * 1.16)
    footer = MARGIN + small_font.size + 62
    y = HEIGHT - footer - len(lines) * line_height

    _badge(draw, (MARGIN, y - 78),
           BAND_LABEL.get(hit.get("decision"), "К СВЕДЕНИЮ"),
           label_font, accent)

    for line in lines:
        draw.text((MARGIN, y), line, font=title_font, fill=(255, 255, 255))
        y += line_height

    # Подпись внизу: площадка слева, источник справа.
    baseline = HEIGHT - MARGIN - small_font.size
    draw.rectangle([MARGIN, baseline + 4, MARGIN + 8, baseline + small_font.size],
                   fill=accent)
    draw.text((MARGIN + 26, baseline), style["label"], font=small_font,
              fill=(255, 255, 255))

    width = draw.textlength(BRAND, font=small_font)
    draw.text((WIDTH - MARGIN - width, baseline), BRAND, font=small_font,
              fill=(190, 190, 200))

    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=88, optimize=True)
    return buffer.getvalue()
