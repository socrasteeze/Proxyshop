"""
* Pokémon compose renderer (Pillow)
"""
# Standard Library Imports
from pathlib import Path
from typing import Optional, Union

# Third Party Imports
from PIL import Image

# Local Imports
from web.shared.compose.frames import load_or_make_pokemon_frame
from web.shared.compose.text import draw_text, paste_art


def _provider(card: dict) -> dict:
    return card.get('provider_data') or card


def _is_trainer(supertype: str) -> bool:
    return 'trainer' in (supertype or '').lower()


def _is_energy(supertype: str) -> bool:
    return 'energy' in (supertype or '').lower()


def _rules_text(p: dict) -> str:
    """Trainer/Energy rules, or fallback attack/ability body text."""
    rules = p.get('rules') or []
    if isinstance(rules, list) and rules:
        return '\n\n'.join(str(r) for r in rules if r)
    if isinstance(rules, str) and rules.strip():
        return rules.strip()
    return ''


def compose_pokemon(
    card: dict,
    art_path: Optional[Union[str, Path]] = None,
    out_path: Optional[Union[str, Path]] = None,
) -> Image.Image:
    """Compose a Pokémon proxy from card metadata + optional art file.

    When art is a full official card scan (the usual pokemontcg.io image),
    the upper art window is cropped out first so cover-fit does not zoom into
    the whole card (frame + rules text).
    """
    p = _provider(card)
    types = list(p.get('types') or [])
    subtypes = list(p.get('subtypes') or [])
    supertype = p.get('supertype') or 'Pokémon'
    trainer = _is_trainer(supertype)
    energy = _is_energy(supertype)

    frame = load_or_make_pokemon_frame(types, subtypes, supertype)
    # Art window matches procedural frame; trainers get a slightly taller window
    if trainer or energy:
        art_box = (48, 140, frame.size[0] - 48, 520)
    else:
        art_box = (48, 140, frame.size[0] - 48, 560)

    if art_path and Path(art_path).is_file():
        try:
            paste_art(frame, Image.open(art_path), art_box, from_full_scan=True)
        except Exception:
            pass

    name = p.get('name') or card.get('name') or 'Unknown'
    hp = str(p.get('hp') or '')
    stage = (subtypes[0] if subtypes else '') or ''

    # Header
    draw_text(frame, stage or (supertype if trainer or energy else ''),
              (48, 48), size=22, fill=(245, 245, 245))
    draw_text(frame, name, (48, 78), size=36, bold=True, fill=(255, 255, 255))
    if hp and not trainer and not energy:
        draw_text(frame, f'HP {hp}', (frame.size[0] - 200, 78), size=32, bold=True,
                  fill=(255, 230, 230))

    y = art_box[3] + 24
    max_w = frame.size[0] - 96

    if trainer or energy:
        rules = _rules_text(p)
        if rules:
            y = draw_text(frame, rules, (48, y), size=22, fill=(230, 230, 230),
                          max_width=max_w)
    else:
        # Ability
        for ab in (p.get('abilities') or [])[:1]:
            draw_text(frame, ab.get('name') or '', (48, y), size=26, bold=True,
                      fill=(220, 200, 120))
            y = draw_text(frame, ab.get('text') or '', (48, y + 34), size=20,
                          fill=(220, 220, 220), max_width=max_w)

        # Attacks
        for atk in (p.get('attacks') or [])[:2]:
            cost = ' '.join(atk.get('cost') or [])
            line = f"{cost}  {atk.get('name') or ''}  {atk.get('damage') or ''}".strip()
            draw_text(frame, line, (48, y), size=24, bold=True, fill=(255, 255, 255))
            y = draw_text(frame, atk.get('text') or '', (48, y + 30), size=18,
                          fill=(200, 200, 200), max_width=max_w)
            y += 8

        # Footer matchups — Pokémon only
        weak = p.get('weaknesses') or []
        resist = p.get('resistances') or []
        retreat = p.get('retreatCost') or []
        weak_s = ', '.join(f"{w.get('type')} {w.get('value')}" for w in weak) or '—'
        resist_s = ', '.join(f"{r.get('type')} {r.get('value')}" for r in resist) or '—'
        retreat_s = str(len(retreat)) if retreat else '0'
        footer = f'Weak: {weak_s}   Resist: {resist_s}   Retreat: {retreat_s}'
        draw_text(frame, footer, (48, frame.size[1] - 90), size=18, fill=(200, 200, 200))

    set_info = p.get('set') or {}
    set_id = set_info.get('id') if isinstance(set_info, dict) else card.get('set')
    num = p.get('number') or card.get('collector_number') or ''
    artist = p.get('artist') or ''
    draw_text(
        frame,
        f'{set_id or ""}  #{num}   {artist}'.strip(),
        (48, frame.size[1] - 55), size=16, fill=(160, 160, 160))

    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        frame.convert('RGB').save(out_path, 'PNG')
    return frame
