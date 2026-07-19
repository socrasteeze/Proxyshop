"""
* Riftbound compose renderer (Pillow)
"""
# Standard Library Imports
from pathlib import Path
from typing import Optional, Union

# Third Party Imports
from PIL import Image

# Local Imports
from web.shared.compose.frames import load_or_make_riftbound_frame, riftbound_art_box
from web.shared.compose.text import (
    apply_bleed, draw_text, frame_style_of, layers_of, paste_art)


def _provider(card: dict) -> dict:
    return card.get('provider_data') or card


def compose_riftbound(
    card: dict,
    art_path: Optional[Union[str, Path]] = None,
    out_path: Optional[Union[str, Path]] = None,
    art_transform: Optional[dict] = None,
    custom_art: bool = False,
    bleed_px: int = 0,
) -> Image.Image:
    """Compose a Riftbound proxy from card metadata + optional art file."""
    p = _provider(card)
    domain = str(p.get('domain') or p.get('faction') or '')
    if domain and domain == domain.lower():
        domain = domain[:1].upper() + domain[1:]
    card_type = str(p.get('cardType') or p.get('type') or '')
    style = frame_style_of(card)
    layers = layers_of(card)

    frame = load_or_make_riftbound_frame(domain, card_type)
    art_box = riftbound_art_box(style)
    if layers['art'] and art_path and Path(art_path).is_file():
        try:
            paste_art(
                frame, Image.open(art_path), art_box,
                from_full_scan=not custom_art,
                transform=art_transform)
        except Exception:
            pass

    name = p.get('name') or card.get('name') or 'Unknown'
    stats = p.get('stats') if isinstance(p.get('stats'), dict) else {}
    energy = p.get('energyCost') if p.get('energyCost') is not None else stats.get('energy')
    power = p.get('powerCost') if p.get('powerCost') is not None else stats.get('power')
    might = p.get('might') if p.get('might') is not None else stats.get('might')

    if layers['text']:
        draw_text(frame, f'{domain}  ·  {card_type}'.strip(' ·'), (48, 40),
                  size=20, fill=(230, 230, 230))
        draw_text(frame, name, (48, 70), size=34, bold=True, fill=(255, 255, 255))

        stats_line = []
        if energy is not None and str(energy) != '':
            stats_line.append(f'Energy {energy}')
        if power is not None and str(power) != '':
            stats_line.append(f'Power {power}')
        if might is not None and str(might) != '':
            stats_line.append(f'Might {might}')
        stats_y = art_box[3] + 20
        if stats_line:
            draw_text(frame, '  ·  '.join(stats_line), (48, stats_y), size=26, bold=True,
                      fill=(255, 220, 180))

        # Strip simple HTML from provider descriptions
        desc = (p.get('description') or p.get('text') or '').replace('<br>', '\n')
        for tag in ('<em>', '</em>', '<i>', '</i>', '<b>', '</b>', '<strong>', '</strong>'):
            desc = desc.replace(tag, '')
        desc = desc.replace('\r', '')
        y = draw_text(frame, desc, (48, stats_y + 50), size=20, fill=(220, 220, 220),
                      max_width=frame.size[0] - 96)

        flavor = (p.get('flavorText') or p.get('flavor_text') or '')
        flavor = flavor.replace('<em>', '').replace('</em>', '')
        if flavor:
            draw_text(frame, flavor, (48, max(y + 12, 880)), size=18,
                      fill=(180, 180, 160), max_width=frame.size[0] - 96)

    if layers['footer']:
        rarity = p.get('rarity') or ''
        code = p.get('code') or p.get('number') or card.get('collector_number') or ''
        set_info = p.get('set') or {}
        set_name = set_info.get('name') if isinstance(set_info, dict) else (
            card.get('set_name') or p.get('set_id') or '')
        draw_text(
            frame,
            f'{set_name or ""}  {code}  {rarity}'.strip(),
            (48, frame.size[1] - 50), size=16, fill=(160, 160, 160))

    frame = apply_bleed(frame, bleed_px)
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        frame.convert('RGB').save(out_path, 'PNG')
    return frame
