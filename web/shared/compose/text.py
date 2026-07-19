"""
* Text helpers for compose renders
"""
# Standard Library Imports
from functools import lru_cache
import re
from typing import Optional

# Third Party Imports
from PIL import Image, ImageDraw, ImageFont

# Scryfall-style symbol → readable glyph for procedural frames
_SYMBOL_MAP = {
    'W': 'W', 'U': 'U', 'B': 'B', 'R': 'R', 'G': 'G', 'C': 'C',
    'T': '⟳', 'Q': 'Untap', 'E': 'E', 'P': 'P', 'S': 'S',
    'X': 'X', 'Y': 'Y', 'Z': 'Z',
    '0': '0', '1': '1', '2': '2', '3': '3', '4': '4', '5': '5',
    '6': '6', '7': '7', '8': '8', '9': '9', '10': '10', '11': '11',
    '12': '12', '13': '13', '14': '14', '15': '15', '16': '16', '20': '20',
}


def expand_symbols(text: str) -> str:
    """Replace {W}/{T}/… mana and tap symbols with plain glyphs."""
    if not text or '{' not in str(text):
        return text or ''

    def repl(m: re.Match) -> str:
        key = m.group(1).upper()
        return _SYMBOL_MAP.get(key, m.group(0))

    return re.sub(r'\{([^}]+)\}', repl, str(text))


def layers_of(card: Optional[dict]) -> dict:
    """Layer visibility flags from card._layers (default all on)."""
    raw = (card or {}).get('_layers') if isinstance(card, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    return {
        'art': bool(raw.get('art', True)),
        'text': bool(raw.get('text', True)),
        'footer': bool(raw.get('footer', True)),
    }


def frame_style_of(card: Optional[dict], default: str = 'default') -> str:
    if not isinstance(card, dict):
        return default
    style = (card.get('frame') or card.get('_frame') or default)
    return str(style).strip().lower() or default


def apply_bleed(img: Image.Image, bleed_px: int = 0) -> Image.Image:
    """Pad image with a dark bleed margin for print (0 = no change)."""
    bleed_px = max(0, min(int(bleed_px or 0), 120))
    if bleed_px <= 0:
        return img
    img = img.convert('RGBA')
    w, h = img.size
    canvas = Image.new('RGBA', (w + 2 * bleed_px, h + 2 * bleed_px), (12, 12, 14, 255))
    canvas.paste(img, (bleed_px, bleed_px), img)
    return canvas


@lru_cache(maxsize=16)
def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Best-effort system font; falls back to Pillow default."""
    candidates = []
    if bold:
        candidates += [
            'C:/Windows/Fonts/arialbd.ttf',
            'C:/Windows/Fonts/segoeuib.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        ]
    candidates += [
        'C:/Windows/Fonts/arial.ttf',
        'C:/Windows/Fonts/segoeui.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_text(
    img: Image.Image,
    text: str,
    xy: tuple[int, int],
    size: int = 28,
    fill: tuple[int, int, int] = (245, 245, 245),
    bold: bool = False,
    max_width: Optional[int] = None,
) -> int:
    """Draw text; wrap if max_width set. Returns y after last line."""
    if not text:
        return xy[1]
    text = expand_symbols(text)
    draw = ImageDraw.Draw(img)
    font = _font(size, bold=bold)
    x, y = xy
    if max_width is None:
        draw.text((x, y), str(text), font=font, fill=fill)
        bbox = font.getbbox(str(text))
        return y + (bbox[3] - bbox[1]) + 6

    words = str(text).replace('\r', '').split()
    lines: list[str] = []
    current = ''
    for word in words:
        trial = f'{current} {word}'.strip()
        if font.getlength(trial) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    # Also split on explicit newlines in original
    if '\n' in str(text) and len(lines) <= 1:
        lines = str(text).split('\n')

    line_h = size + 6
    for i, line in enumerate(lines[:12]):
        draw.text((x, y + i * line_h), line, font=font, fill=fill)
    return y + min(len(lines), 12) * line_h


def normalize_art_transform(transform: Optional[dict] = None) -> dict:
    """Clamp pan/zoom for the art window. scale≥1; offsets in [-1, 1]."""
    if not isinstance(transform, dict):
        return {'scale': 1.0, 'offset_x': 0.0, 'offset_y': 0.0}
    try:
        scale = float(transform.get('scale', 1.0) or 1.0)
    except (TypeError, ValueError):
        scale = 1.0
    try:
        ox = float(transform.get('offset_x', 0.0) or 0.0)
    except (TypeError, ValueError):
        ox = 0.0
    try:
        oy = float(transform.get('offset_y', 0.0) or 0.0)
    except (TypeError, ValueError):
        oy = 0.0
    return {
        'scale': max(1.0, min(scale, 4.0)),
        'offset_x': max(-1.0, min(ox, 1.0)),
        'offset_y': max(-1.0, min(oy, 1.0)),
    }


def paste_cover(
    base: Image.Image,
    art: Image.Image,
    box: tuple[int, int, int, int],
    transform: Optional[dict] = None,
) -> None:
    """Scale-and-crop art into box (cover fit), with optional pan/zoom."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    art = art.convert('RGBA')
    aw, ah = art.size
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return
    t = normalize_art_transform(transform)
    scale = max(bw / aw, bh / ah) * t['scale']
    nw, nh = max(1, int(aw * scale)), max(1, int(ah * scale))
    art = art.resize((nw, nh), Image.Resampling.LANCZOS)
    max_left = max(0, nw - bw)
    max_top = max(0, nh - bh)
    # offset 0 = centered; -1 = top/left edge; +1 = bottom/right edge
    left = int(max_left * (0.5 + 0.5 * t['offset_x']))
    top = int(max_top * (0.5 + 0.5 * t['offset_y']))
    left = max(0, min(left, max_left))
    top = max(0, min(top, max_top))
    cropped = art.crop((left, top, left + bw, top + bh))
    base.paste(cropped, (x0, y0), cropped)


def looks_like_full_card_scan(art: Image.Image, card_aspect: float = 750 / 1050) -> bool:
    """True when the image is portrait and near standard card proportions."""
    aw, ah = art.size
    if aw <= 0 or ah <= 0 or ah < aw:
        return False
    aspect = aw / ah
    return abs(aspect - card_aspect) < 0.09


def extract_card_art_region(art: Image.Image) -> Image.Image:
    """Crop the usual upper art window out of a full TCG card scan.

    Official Pokémon/Riftbound scans include frame + rules; cover-fitting the
    whole scan into a blank-frame art hole is what causes the clipped look.
    """
    art = art.convert('RGBA')
    if not looks_like_full_card_scan(art):
        return art
    w, h = art.size
    # Typical modern TCG art window (fractions of full card)
    x0 = int(w * 0.075)
    y0 = int(h * 0.115)
    x1 = int(w * 0.925)
    y1 = int(h * 0.515)
    if x1 <= x0 + 8 or y1 <= y0 + 8:
        return art
    return art.crop((x0, y0, x1, y1))


def paste_art(
    base: Image.Image,
    art: Image.Image,
    box: tuple[int, int, int, int],
    *,
    from_full_scan: bool = True,
    transform: Optional[dict] = None,
) -> None:
    """Paste art into box; optionally peel art out of a full-card scan first."""
    if from_full_scan:
        art = extract_card_art_region(art)
    paste_cover(base, art, box, transform=transform)

