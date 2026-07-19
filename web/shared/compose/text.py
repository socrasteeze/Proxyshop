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
    scale = max(bw / aw, bh / ah)
    nw, nh = int(aw * scale), int(ah * scale)
    art = art.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - bw) // 2
    top = (nh - bh) // 2
    cropped = art.crop((left, top, left + bw, top + bh))
    base.paste(cropped, (x0, y0), cropped)
