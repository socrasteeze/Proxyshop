"""
* MTG compose renderer (Pillow) — pokecardmaker-style blank + art + text
"""
# Standard Library Imports
from pathlib import Path
from typing import Optional, Union

# Third Party Imports
from PIL import Image

# Local Imports
from web.shared.compose import CARD_H, CARD_W
from web.shared.compose.frames import FRAMES_ROOT, _procedural_frame, mtg_art_box
from web.shared.compose.text import (
    apply_bleed, draw_text, frame_style_of, layers_of, paste_cover)

MTG_COLOR_MAP: dict[str, tuple[int, int, int]] = {
    'W': (230, 220, 180),
    'U': (64, 144, 200),
    'B': (56, 56, 64),
    'R': (196, 72, 56),
    'G': (72, 144, 72),
    'C': (160, 160, 168),
    'M': (200, 168, 64),  # multicolor / gold
}


def _accent_for_card(card: dict) -> tuple[int, int, int]:
    colors = card.get('colors') or card.get('color_identity') or []
    if isinstance(colors, str):
        colors = list(colors)
    if len(colors) == 0:
        # Lands / artifacts often colorless
        type_line = (card.get('type_line') or '').lower()
        if 'land' in type_line:
            return (120, 100, 72)
        return MTG_COLOR_MAP['C']
    if len(colors) == 1:
        return MTG_COLOR_MAP.get(colors[0], MTG_COLOR_MAP['C'])
    return MTG_COLOR_MAP['M']


def load_or_make_mtg_frame(card: dict, style: str = 'default') -> Image.Image:
    style = (style or 'default').lower()
    for name in (style, 'default'):
        path = FRAMES_ROOT / 'mtg' / f'{name}.png'
        if path.is_file():
            return Image.open(path).convert('RGBA').resize(
                (CARD_W, CARD_H), Image.Resampling.LANCZOS)
    return _procedural_frame(_accent_for_card(card), art_box=mtg_art_box(style))


def compose_mtg(
    card: dict,
    art_path: Optional[Union[str, Path]] = None,
    out_path: Optional[Union[str, Path]] = None,
    art_transform: Optional[dict] = None,
    custom_art: bool = False,  # noqa: ARG001 — parity with other games
    bleed_px: int = 0,
) -> Image.Image:
    """Compose an MTG-style proxy from Scryfall-shaped card data + optional art."""
    # Prefer front face for DFCs when present
    face = card
    faces = card.get('card_faces') or []
    if faces and isinstance(faces[0], dict):
        face = {**card, **faces[0]}

    style = frame_style_of(card)
    layers = layers_of(card)
    frame = load_or_make_mtg_frame(face if face.get('colors') is not None else card, style)
    art_box = mtg_art_box(style)
    if layers['art'] and art_path and Path(art_path).is_file():
        try:
            paste_cover(frame, Image.open(art_path), art_box, transform=art_transform)
        except Exception:
            pass

    name = face.get('name') or card.get('name') or 'Unknown'
    mana = face.get('mana_cost') or card.get('mana_cost') or ''
    type_line = face.get('type_line') or card.get('type_line') or ''
    oracle = face.get('oracle_text') or card.get('oracle_text') or ''
    power = face.get('power') if face.get('power') is not None else card.get('power')
    toughness = face.get('toughness') if face.get('toughness') is not None else card.get('toughness')
    artist = face.get('artist') or card.get('artist') or ''
    set_code = (card.get('set') or '').upper()
    number = card.get('collector_number') or ''

    if layers['text']:
        name_y = 48 if style == 'default' else 28
        draw_text(frame, name, (48, name_y), size=32, bold=True, fill=(255, 255, 255),
                  max_width=frame.size[0] - 220)
        if mana:
            draw_text(frame, mana, (frame.size[0] - 200, name_y + 4), size=24, bold=True,
                      fill=(255, 240, 200))

        type_y = 575 if style == 'default' else art_box[3] + 12
        draw_text(frame, type_line, (48, type_y), size=22, bold=True, fill=(235, 235, 235),
                  max_width=frame.size[0] - 96)

        oracle_y = type_y + 45
        draw_text(frame, oracle, (48, oracle_y), size=20, fill=(220, 220, 220),
                  max_width=frame.size[0] - 96)

        if power is not None and toughness is not None:
            draw_text(frame, f'{power}/{toughness}',
                      (frame.size[0] - 160, frame.size[1] - 100),
                      size=36, bold=True, fill=(255, 255, 255))

    if layers['footer']:
        draw_text(
            frame,
            f'{set_code}  #{number}   {artist}'.strip(),
            (48, frame.size[1] - 50), size=16, fill=(160, 160, 160))

    frame = apply_bleed(frame, bleed_px)
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        frame.convert('RGB').save(out_path, 'PNG')
    return frame
