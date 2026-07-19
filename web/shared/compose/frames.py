"""
* Frame resolution + procedural fallbacks
* Optional blank PNGs live under frames/<game>/… ; if missing, a colored
* procedural frame is generated so compose always works.
"""
# Standard Library Imports
from pathlib import Path
from typing import Optional

# Third Party Imports
from PIL import Image, ImageDraw

# Local Imports
from web.shared.compose import CARD_H, CARD_W, FRAMES_ROOT

# Pokémon type → accent color
POKEMON_TYPE_COLORS: dict[str, tuple[int, int, int]] = {
    'Grass': (88, 168, 80),
    'Fire': (220, 96, 64),
    'Water': (64, 144, 220),
    'Lightning': (240, 200, 48),
    'Psychic': (168, 88, 184),
    'Fighting': (176, 104, 56),
    'Darkness': (72, 72, 96),
    'Metal': (144, 152, 168),
    'Fairy': (224, 136, 184),
    'Dragon': (112, 88, 168),
    'Colorless': (168, 168, 160),
}

# Riftbound domain → accent
RIFTBOUND_DOMAIN_COLORS: dict[str, tuple[int, int, int]] = {
    'Fury': (196, 64, 48),
    'Calm': (64, 144, 168),
    'Mind': (88, 112, 196),
    'Body': (168, 120, 48),
    'Chaos': (136, 64, 168),
    'Order': (80, 144, 96),
}


def mtg_art_box(style: str = 'default') -> tuple[int, int, int, int]:
    style = (style or 'default').lower()
    if style == 'borderless':
        return (20, 90, CARD_W - 20, 720)
    if style in ('fullart', 'extended', 'textless'):
        return (16, 16, CARD_W - 16, CARD_H - 120)
    return (48, 130, CARD_W - 48, 560)


def pokemon_art_box(style: str = 'default', *, trainer: bool = False) -> tuple[int, int, int, int]:
    style = (style or 'default').lower()
    if style in ('fullart', 'borderless'):
        return (20, 100, CARD_W - 20, 700)
    if trainer:
        return (48, 140, CARD_W - 48, 520)
    return (48, 140, CARD_W - 48, 560)


def riftbound_art_box(style: str = 'default') -> tuple[int, int, int, int]:
    style = (style or 'default').lower()
    if style in ('wide', 'borderless', 'fullart'):
        return (20, 90, CARD_W - 20, 720)
    return (40, 120, CARD_W - 40, 620)


FRAME_STYLES: dict[str, list[str]] = {
    'mtg': ['default', 'borderless', 'fullart'],
    'pokemon': ['default', 'fullart'],
    'riftbound': ['default', 'wide'],
}


def _slug(value: str) -> str:
    return ''.join(c if c.isalnum() else '_' for c in (value or '').strip().lower())


def pokemon_frame_path(
    types: list[str] | None = None,
    subtypes: list[str] | None = None,
    supertype: str = 'Pokémon',
) -> Optional[Path]:
    """Look for an optional blank PNG; return None to use procedural."""
    type_name = (types or ['Colorless'])[0]
    subtype = (subtypes or ['Basic'])[0]
    st = 'trainer' if 'trainer' in (supertype or '').lower() else (
        'energy' if 'energy' in (supertype or '').lower() else 'pokemon')
    candidates = [
        FRAMES_ROOT / 'pokemon' / st / f'{_slug(type_name)}_{_slug(subtype)}.png',
        FRAMES_ROOT / 'pokemon' / st / f'{_slug(type_name)}.png',
        FRAMES_ROOT / 'pokemon' / f'{_slug(type_name)}.png',
        FRAMES_ROOT / 'pokemon' / 'default.png',
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def riftbound_frame_path(domain: str = '', card_type: str = '') -> Optional[Path]:
    candidates = [
        FRAMES_ROOT / 'riftbound' / f'{_slug(domain)}_{_slug(card_type)}.png',
        FRAMES_ROOT / 'riftbound' / f'{_slug(domain)}.png',
        FRAMES_ROOT / 'riftbound' / 'default.png',
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def procedural_pokemon_frame(
    types: list[str] | None = None,
    supertype: str = 'Pokémon',
) -> Image.Image:
    """Generate a simple blank frame (art window + bars) tinted by type."""
    type_name = (types or ['Colorless'])[0]
    if 'trainer' in (supertype or '').lower():
        accent = (72, 112, 168)
    elif 'energy' in (supertype or '').lower():
        accent = POKEMON_TYPE_COLORS.get(type_name, (168, 168, 160))
    else:
        accent = POKEMON_TYPE_COLORS.get(type_name, (168, 168, 160))
    return _procedural_frame(accent, art_box=(48, 140, CARD_W - 48, 560))


def procedural_riftbound_frame(domain: str = '') -> Image.Image:
    accent = RIFTBOUND_DOMAIN_COLORS.get(domain, (96, 96, 112))
    return _procedural_frame(accent, art_box=(40, 120, CARD_W - 40, 620))


def _procedural_frame(
    accent: tuple[int, int, int],
    art_box: tuple[int, int, int, int],
) -> Image.Image:
    img = Image.new('RGBA', (CARD_W, CARD_H), (28, 28, 32, 255))
    draw = ImageDraw.Draw(img)
    # Outer border
    draw.rectangle([12, 12, CARD_W - 13, CARD_H - 13], outline=accent, width=10)
    # Inner panel
    draw.rectangle([28, 28, CARD_W - 29, CARD_H - 29], outline=(48, 48, 52), width=2)
    # Art window (transparent hole → filled later with art)
    ax0, ay0, ax1, ay1 = art_box
    draw.rectangle([ax0, ay0, ax1, ay1], fill=(16, 16, 18, 255), outline=accent, width=3)
    # Header / footer bars
    draw.rectangle([36, 36, CARD_W - 37, ay0 - 8], fill=(accent[0], accent[1], accent[2], 220))
    draw.rectangle([36, ay1 + 8, CARD_W - 37, CARD_H - 36], fill=(36, 36, 40, 240))
    return img


def load_or_make_pokemon_frame(
    types: list[str] | None = None,
    subtypes: list[str] | None = None,
    supertype: str = 'Pokémon',
) -> Image.Image:
    path = pokemon_frame_path(types, subtypes, supertype)
    if path:
        return Image.open(path).convert('RGBA').resize((CARD_W, CARD_H), Image.Resampling.LANCZOS)
    return procedural_pokemon_frame(types, supertype)


def load_or_make_riftbound_frame(domain: str = '', card_type: str = '') -> Image.Image:
    path = riftbound_frame_path(domain, card_type)
    if path:
        return Image.open(path).convert('RGBA').resize((CARD_W, CARD_H), Image.Resampling.LANCZOS)
    return procedural_riftbound_frame(domain)
