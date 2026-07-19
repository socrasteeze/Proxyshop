"""
* Text helpers for compose renders
"""
# Standard Library Imports
from functools import lru_cache
from typing import Optional

# Third Party Imports
from PIL import Image, ImageDraw, ImageFont


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


def paste_cover(
    base: Image.Image,
    art: Image.Image,
    box: tuple[int, int, int, int],
) -> None:
    """Scale-and-crop art into box (cover fit)."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    art = art.convert('RGBA')
    aw, ah = art.size
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return
    scale = max(bw / aw, bh / ah)
    nw, nh = max(1, int(aw * scale)), max(1, int(ah * scale))
    art = art.resize((nw, nh), Image.Resampling.LANCZOS)
    left = max(0, (nw - bw) // 2)
    top = max(0, (nh - bh) // 2)
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
) -> None:
    """Paste art into box; optionally peel art out of a full-card scan first."""
    if from_full_scan:
        art = extract_card_art_region(art)
    paste_cover(base, art, box)

